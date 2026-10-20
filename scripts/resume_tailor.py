import os
import sys
import shutil
import subprocess
from pathlib import Path

# The LaTeX resume project and the Tectonic binary are machine-specific, so
# they are read from the environment instead of being hardcoded:
#   RESUME_PROJECT_DIR  — directory containing resume_optimized.tex and
#                         src/achievements.tex
#   TECTONIC_BIN        — path to the tectonic executable (falls back to PATH)
RESUME_PROJECT_DIR = Path(os.environ.get("RESUME_PROJECT_DIR", "./resume")).expanduser()
ACHIEVEMENTS_FILE = RESUME_PROJECT_DIR / "src/achievements.tex"
TECTONIC_BIN = os.environ.get("TECTONIC_BIN") or shutil.which("tectonic") or "tectonic"
MANDATORY_LINE = r"\resumeItem{IICPC Regionalist with 56th rank in regionals and 276th rank in Prelims 2025.}"

def tailor_resume(job_description):
    print(f"Tailoring resume for: {job_description[:50]}...")

    if not ACHIEVEMENTS_FILE.exists():
        print(f"Error: {ACHIEVEMENTS_FILE} not found.")
        print("Set RESUME_PROJECT_DIR to your LaTeX resume project directory.")
        return False
    
    with open(ACHIEVEMENTS_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    
    if MANDATORY_LINE not in content:
        if r"\begin{itemize}" in content:
            content = content.replace(r"\begin{itemize}", r"\begin{itemize}" + "\n    " + MANDATORY_LINE)
        else:
            content = r"\begin{itemize}" + "\n    " + MANDATORY_LINE + "\n    " + content + "\n" + r"\end{itemize}"

    with open(ACHIEVEMENTS_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    
    try:
        result = subprocess.run(
            [str(TECTONIC_BIN), "resume_optimized.tex"], 
            cwd=str(RESUME_PROJECT_DIR),
            capture_output=True, 
            text=True,
            encoding="utf-8"
        )
        if result.returncode == 0:
            print("Resume compiled successfully.")
            return True
        else:
            print(f"Tectonic Error: {result.stderr}")
            return False
    except Exception as e:
        print(f"Exception: {e}")
        return False

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(1)
    sys.exit(0 if tailor_resume(sys.argv[1]) else 1)
