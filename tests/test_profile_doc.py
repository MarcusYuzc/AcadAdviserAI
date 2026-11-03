from __future__ import annotations

import io
import zipfile

from acadadviser.profile_doc import typed_profile_docx


def test_docx_escapes_text_and_keeps_lines():
    data = typed_profile_docx("GPA 3.8 <top> & co\n\n数学专业")
    assert data.startswith(b"PK\x03\x04")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        document = archive.read("word/document.xml").decode()
    assert "GPA 3.8 &lt;top&gt; &amp; co" in document
    assert "数学专业" in document
    assert document.count("<w:p>") == 2
