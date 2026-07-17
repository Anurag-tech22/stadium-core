import subprocess

print("Running mypy...")
res = subprocess.run([r".venv\Scripts\mypy.exe", "app/", "--ignore-missing-imports"], capture_output=True, text=True)
print("STDOUT:")
print(res.stdout)
print("STDERR:")
print(res.stderr)
print(f"Exit code: {res.returncode}")
