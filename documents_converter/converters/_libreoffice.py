"""
Shared LibreOffice-headless conversion helper (master directive Phase 9:
"Core Conversion Engine") -- office_to_pdf.py, html_to_pdf.py, and
markdown_to_pdf.py (via its own HTML intermediate) all funnel through
this one function rather than each shelling out to `soffice` themselves.

LibreOffice is a deliberately heavy dependency, chosen with that
tradeoff explicit rather than reached for by default: there is no
lightweight pure-Python option that renders real .docx/.xlsx/.pptx files
correctly, so this project accepts a real document-rendering engine (a
system binary, not a pip package) as a cost of that capability actually
working.

Leading underscore: this module has no CAPABILITY of its own and isn't
meant to be imported outside documents_converter/converters/.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def check_libreoffice_available(soffice_cmd: str | None = None) -> bool:
    if soffice_cmd:
        return Path(soffice_cmd).is_file()
    return shutil.which("soffice") is not None


def convert_via_libreoffice(
    input_path: Path,
    output_path: Path,
    soffice_cmd: str | None = None,
    timeout: float = 120,
) -> None:
    """
    Converts `input_path` to a PDF at `output_path` by shelling out to
    LibreOffice's own headless conversion mode.

    Two LibreOffice quirks handled here, not left to bite the first
    concurrent request:
    - It only lets you choose an output *directory*, not an exact output
      filename -- it always names the result after the input file's own
      stem. Run against an isolated temp directory and move the result
      to `output_path` itself.
    - Multiple headless instances sharing the default user profile
      directory fail against each other ("another instance is already
      running") -- a well-documented LibreOffice limitation, not
      specific to this project. This service's job queue runs
      conversions concurrently (api/app.py's _convert_executor, up to 4
      at once), so each call gets its own throwaway profile directory
      via -env:UserInstallation.
    """
    soffice = soffice_cmd or "soffice"
    if not check_libreoffice_available(soffice_cmd):
        raise EnvironmentError(
            "LibreOffice (soffice) not found on PATH. Install it first, or "
            "set LIBREOFFICE_CMD to its full path."
        )

    with (
        tempfile.TemporaryDirectory(prefix="soffice-out-") as out_dir,
        tempfile.TemporaryDirectory(prefix="soffice-profile-") as profile_dir,
    ):
        profile_uri = Path(profile_dir).resolve().as_uri()
        try:
            result = subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--norestore",
                    f"-env:UserInstallation={profile_uri}",
                    "--convert-to", "pdf",
                    "--outdir", out_dir,
                    str(input_path),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"LibreOffice conversion exceeded {timeout:.0f}s and was abandoned."
            ) from e

        if result.returncode != 0:
            raise RuntimeError(
                f"LibreOffice conversion failed (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

        produced = Path(out_dir) / f"{input_path.stem}.pdf"
        if not produced.exists():
            raise RuntimeError(
                "LibreOffice reported success but did not produce the expected "
                f"output file ({produced.name})."
            )
        shutil.move(str(produced), str(output_path))
