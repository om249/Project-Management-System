import os
import subprocess

def convert_to_pdf(input_path, output_folder):

    try:
        # LibreOffice command
        subprocess.run([
            "soffice",
            "--headless",
            "--convert-to", "pdf",
            "--outdir", output_folder,
            input_path
        ], check=True)

        base = os.path.splitext(os.path.basename(input_path))[0]
        return base + ".pdf"

    except Exception as e:
        print("Conversion Error:", e)

        # If already PDF → return same file
        if input_path.endswith(".pdf"):
            return os.path.basename(input_path)

        return None