"""
PDF security (master directive Phase 12: "Document Security" --
password protection, encryption, redaction, signing, permissions).

Password protection, encryption, and permissions are one PyMuPDF
feature under the hood (the PDF standard security handler): no new
dependency, same as pdf_utilities.py. Redaction is also pure PyMuPDF,
via its native redact-annotation mechanism, which permanently removes
the underlying content (text and images) within a region -- not a
black rectangle drawn on top of content that is still there underneath.

Digital signing is a genuinely different kind of feature: real,
tamper-evident signing needs a cryptographic signing library, not just
a PDF-manipulation one. This brings in pyHanko (a real, maintained
PDF-signing/validation library) plus `cryptography` (already one of
pyHanko's own dependencies) to generate an ephemeral demo certificate.
See sign_pdf() and generate_demo_signer()'s docstrings for exactly what
trust that certificate does and doesn't provide -- disclosed here, not
left to be discovered the hard way in production.
"""

from __future__ import annotations

import datetime
import logging
import threading
from pathlib import Path

import fitz

# pyhanko_certvalidator logs a full traceback, at ERROR level, every
# time it fails to build a trust path to a root -- which is the
# *expected*, every-single-call outcome for the self-signed demo
# certificate this module generates (there is no root to chain to on
# purpose). Left at the default level, one /verify call on a
# demo-signed PDF would look like a crash in the container logs.
# Scoped to just these two logger namespaces so a genuine, unexpected
# error anywhere else in the app is never silenced by this.
logging.getLogger("pyhanko_certvalidator").setLevel(logging.CRITICAL)
logging.getLogger("pyhanko").setLevel(logging.CRITICAL)

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives.serialization import pkcs12  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter  # noqa: E402
from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: E402
from pyhanko.sign import fields, signers  # noqa: E402
from pyhanko.sign.diff_analysis import ModificationLevel  # noqa: E402
from pyhanko.sign.signers.pdf_signer import PdfSignatureMetadata  # noqa: E402
from pyhanko.sign.validation import validate_pdf_signature  # noqa: E402
from pyhanko_certvalidator import ValidationContext  # noqa: E402

# --------------------------------------------------------------------------
# Password protection, encryption, permissions (PyMuPDF standard security
# handler -- one feature covering all three).
# --------------------------------------------------------------------------

#: Friendly names exposed at the API layer, mapped to PyMuPDF's own
#: permission bits. Deliberately a fixed, spelled-out set rather than
#: accepting a raw integer bitmask from callers -- a typo'd bitmask is a
#: silent security bug, a typo'd name is a loud 400.
PERMISSION_NAMES: dict[str, int] = {
    "print": fitz.PDF_PERM_PRINT,
    "print_hq": fitz.PDF_PERM_PRINT_HQ,
    "modify": fitz.PDF_PERM_MODIFY,
    "copy": fitz.PDF_PERM_COPY,
    "annotate": fitz.PDF_PERM_ANNOTATE,
    "form": fitz.PDF_PERM_FORM,
    "accessibility": fitz.PDF_PERM_ACCESSIBILITY,
    "assemble": fitz.PDF_PERM_ASSEMBLE,
}


def parse_permissions(names: list[str]) -> int:
    """Converts friendly permission names (e.g. ["print", "copy"]) into
    the OR'd PyMuPDF bitmask. Unknown names raise rather than being
    silently ignored -- a permission the caller thinks they granted but
    didn't (because of a typo) is a security-relevant surprise."""
    mask = 0
    for name in names:
        key = name.strip().lower()
        if key not in PERMISSION_NAMES:
            raise ValueError(
                f"Unknown permission '{name}'. Valid values: "
                f"{', '.join(sorted(PERMISSION_NAMES))}."
            )
        mask |= PERMISSION_NAMES[key]
    return mask


def protect_pdf(
    input_path: Path,
    output_path: Path,
    user_password: str | None = None,
    owner_password: str | None = None,
    permissions: int | None = None,
) -> None:
    """
    Encrypts the PDF (AES-256, the modern PDF standard security handler)
    with a user password (required to open the file at all), an owner
    password (required to change security settings or exceed the
    granted permissions), or both.

    At least one password is required -- encryption with neither is not
    a real security feature (no password handler behaves any
    differently from an unprotected file in that case).

    `permissions` (an OR'd mask from parse_permissions(), or None for
    "no restrictions") only has teeth once there's an owner password to
    protect it: a permission mask with only a user password guarding it
    can be bypassed by simply removing the user password requirement
    with a PDF tool that doesn't enforce it, since there is nothing an
    owner-level actor is being asked to prove. When only `owner_password`
    is given (no `user_password`), the file opens freely but a
    conforming reader restricts what can be done with it -- a real,
    common use case (e.g. "anyone can view, nobody can print"), not a
    fallback.
    """
    if not user_password and not owner_password:
        raise ValueError(
            "At least one of user_password or owner_password is required "
            "-- encryption with neither password set protects nothing."
        )
    # PyMuPDF requires an owner password whenever a user password is set
    # (it refuses a user-only encryption where anyone could authenticate
    # as owner with an empty string); mirror the common real-world
    # default of reusing the user password as the owner password rather
    # than silently granting full owner rights to an empty string.
    effective_owner_password = owner_password or user_password

    doc = fitz.open(str(input_path))
    try:
        doc.save(
            str(output_path),
            encryption=fitz.PDF_ENCRYPT_AES_256,
            user_pw=user_password or "",
            owner_pw=effective_owner_password,
            permissions=permissions if permissions is not None else -1,
        )
    finally:
        doc.close()


def remove_protection(input_path: Path, output_path: Path, password: str) -> None:
    """Decrypts a password-protected PDF given a password that
    successfully authenticates (as either user or owner)."""
    doc = fitz.open(str(input_path))
    try:
        if not doc.is_encrypted:
            raise ValueError("This PDF is not password-protected.")
        if not doc.authenticate(password):
            raise ValueError("Incorrect password.")
        doc.save(str(output_path), encryption=fitz.PDF_ENCRYPT_NONE)
    finally:
        doc.close()


# --------------------------------------------------------------------------
# Redaction -- permanent content removal (PyMuPDF's native redact
# annotations), not a visual-only overlay.
# --------------------------------------------------------------------------


def redact_pdf(
    input_path: Path,
    output_path: Path,
    terms: list[str] | None = None,
    rects: list[tuple[int, float, float, float, float]] | None = None,
) -> int:
    """
    Permanently removes content from the PDF. Two independent selection
    modes, usable together:
    - `terms`: every case-sensitive occurrence of each string, found via
      PyMuPDF's own text search on every page.
    - `rects`: explicit regions as (0-indexed page, x0, y0, x1, y1) in
      PDF points (the same top-left-origin coordinate space as
      `page.rect` elsewhere in this project, e.g. pdf_utilities.crop_pages).

    Returns the number of regions actually redacted. Raises ValueError
    if neither `terms` nor `rects` is given, or if `terms` matches
    nothing anywhere in the document -- a redaction request that
    silently redacts zero occurrences of its search term is far more
    likely a caller mistake (wrong case, wrong text) than an
    intentional no-op, and this is a security operation where a
    caller's false confidence ("I redacted the SSN") is a real harm.
    """
    if not terms and not rects:
        raise ValueError("At least one of `terms` or `rects` is required.")

    doc = fitz.open(str(input_path))
    try:
        count = 0
        if terms:
            for page in doc:
                for term in terms:
                    for rect in page.search_for(term):
                        page.add_redact_annot(rect, fill=(0, 0, 0))
                        count += 1
        if rects:
            for page_index, x0, y0, x1, y1 in rects:
                if page_index < 0 or page_index >= len(doc):
                    raise ValueError(
                        f"Page {page_index + 1} is out of range "
                        f"(document has {len(doc)} pages)."
                    )
                page = doc[page_index]
                page.add_redact_annot(fitz.Rect(x0, y0, x1, y1), fill=(0, 0, 0))
                count += 1
        if count == 0:
            raise ValueError(
                "No matches found for the given search term(s) -- nothing "
                "was redacted."
            )
        for page in doc:
            page.apply_redactions()
        doc.save(str(output_path))
        return count
    finally:
        doc.close()


# --------------------------------------------------------------------------
# Digital signing (pyHanko) -- real, tamper-evident cryptographic
# signatures, not a visual signature stamp.
# --------------------------------------------------------------------------

_demo_signer: signers.SimpleSigner | None = None
_demo_signer_lock = threading.Lock()


def generate_demo_signer() -> signers.SimpleSigner:
    """
    Returns this process's demo signing identity: a 2048-bit RSA key and
    a self-signed X.509 certificate, generated once in memory the first
    time signing is requested and reused for the rest of the process's
    life. Never written to disk, and never the same across restarts.

    What this actually buys a signed PDF: genuine tamper-evidence --
    pyHanko/PyMuPDF's own byte-range signature mechanism means any edit
    to the signed content after signing is cryptographically detectable
    (see verify_pdf()). What it does NOT buy: identity trust. A
    self-signed certificate has no chain to any trust root, so a
    verifier has no basis to believe "Documents Converter Demo Signer"
    actually refers to this deployment, let alone a specific real
    person or organization -- verify_pdf() reports `trusted: False` for
    exactly this reason, on every signature this function produces, and
    that is correct, not a bug to silence. Real identity-bound signing
    needs a certificate issued by a CA the intended verifiers already
    trust -- pass one via load_pkcs12_signer() instead once you have
    one.
    """
    global _demo_signer
    with _demo_signer_lock:
        if _demo_signer is not None:
            return _demo_signer

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name(
            [
                x509.NameAttribute(
                    NameOID.COMMON_NAME, "Documents Converter Demo Signer"
                ),
                x509.NameAttribute(
                    NameOID.ORGANIZATIONAL_UNIT_NAME,
                    "NOT FOR PRODUCTION USE -- self-signed, ephemeral",
                ),
            ]
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .sign(key, hashes.SHA256())
        )
        pkcs12_bytes = pkcs12.serialize_key_and_certificates(
            name=b"documents-converter-demo",
            key=key,
            cert=cert,
            cas=None,
            encryption_algorithm=serialization.NoEncryption(),
        )
        signer = signers.SimpleSigner.load_pkcs12_data(pkcs12_bytes, other_certs=[])
        if signer is None:  # pragma: nocover -- would mean pyHanko rejected its own output
            raise RuntimeError("Failed to construct the demo signing certificate.")
        _demo_signer = signer
        return signer


def load_pkcs12_signer(data: bytes, passphrase: str | None = None) -> signers.SimpleSigner:
    """Builds a signer from a caller-supplied PKCS#12 (.pfx/.p12)
    certificate+key bundle -- for real, identity-bound signing once an
    organization has a certificate from a CA its verifiers trust."""
    signer = signers.SimpleSigner.load_pkcs12_data(
        data,
        other_certs=[],
        passphrase=passphrase.encode("utf-8") if passphrase else None,
    )
    if signer is None:
        raise ValueError(
            "Could not load the PKCS#12 file -- check the file and passphrase."
        )
    return signer


def sign_pdf(
    input_path: Path,
    output_path: Path,
    signer: signers.SimpleSigner,
    reason: str | None = None,
    location: str | None = None,
    field_name: str = "Signature1",
) -> None:
    """Adds a real, cryptographic digital signature to the PDF via an
    incremental update (the original bytes are untouched -- pyHanko
    appends the new signature field and its signature as a new
    revision, which is how PDF signing always works and how multiple
    independent signatures can coexist on one file)."""
    with open(input_path, "rb") as f:
        writer = IncrementalPdfFileWriter(f)
        fields.append_signature_field(
            writer, sig_field_spec=fields.SigFieldSpec(sig_field_name=field_name)
        )
        meta = PdfSignatureMetadata(field_name=field_name, reason=reason, location=location)
        result = signers.sign_pdf(writer, meta, signer=signer)
        output_path.write_bytes(result.getvalue())


def verify_pdf(input_path: Path) -> list[dict]:
    """
    Reports on every digital signature embedded in the PDF. For each:
    `field_name`, `signer_cn` (the signing certificate's Common Name),
    `reason`, `location`, `intact` (the signed bytes are byte-for-byte
    unchanged since signing), `trusted` (the signing certificate chains
    to a trust root -- always False for generate_demo_signer()'s
    certificates, by design; see its docstring), `modified_after_signing`
    (a later revision changed something pyHanko's diff analysis can't
    attribute to a benign cause, e.g. a new signature or annotation --
    the tamper-evidence signal, independent of `trusted`: it stays
    accurate even for an untrusted signer, which is why this is a
    separate field rather than folded into one combined verdict), and
    `bottom_line` (pyHanko's overall verdict: intact AND trusted AND not
    modified_after_signing -- for a demo-signed PDF this is always False
    regardless of tampering, since `trusted` alone already forces it,
    which is exactly why `modified_after_signing` exists as its own
    field here rather than leaving tamper-evidence undetectable behind
    an always-False bottom line). Returns an empty list for a PDF with
    no signatures -- that is a normal, valid result, not an error.

    Deliberately fully offline: no revocation/CRL/OCSP fetching (a
    verifier reaching out to a third-party URL embedded in someone
    else's uploaded file is its own can of worms), so `trusted` reflects
    only whether a path to one of *this call's* (empty, on purpose)
    trust roots exists -- never true today. A real deployment that
    wants signatures from a specific known CA to verify as trusted would
    pass that CA's certificate as a trust root here.
    """
    results = []
    with open(input_path, "rb") as f:
        reader = PdfFileReader(f)
        vc = ValidationContext(trust_roots=[], allow_fetching=False)
        for sig in reader.embedded_signatures:
            status = validate_pdf_signature(sig, signer_validation_context=vc, skip_diff=False)
            results.append(
                {
                    "field_name": sig.field_name,
                    "signer_cn": sig.signer_cert.subject.native.get("common_name"),
                    "reason": sig.sig_object.get("/Reason"),
                    "location": sig.sig_object.get("/Location"),
                    "intact": bool(status.intact),
                    "trusted": bool(status.trusted),
                    "modified_after_signing": status.modification_level != ModificationLevel.NONE,
                    "bottom_line": bool(status.bottom_line),
                }
            )
    return results
