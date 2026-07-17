import subprocess
import sys

# Kill any locked processes using pytest or ruff in the background
subprocess.run(["taskkill", "/F", "/IM", "pytest.exe"], capture_output=True)
subprocess.run(["taskkill", "/F", "/IM", "ruff.exe"], capture_output=True)

print("Installing requirements-dev.txt dependencies locally...")
res = subprocess.run([r".venv\Scripts\python.exe", "-m", "pip", "install", "-r", "requirements-dev.txt"], capture_output=True, text=True)
print("STDOUT:")
print(res.stdout[:2000])  # print first 2000 chars of stdout
print("STDERR:")
print(res.stderr)
print(f"Exit code: {res.returncode}")

if res.returncode == 0:
    print("\nRunning Ruff format on all files with updated version...")
    res2 = subprocess.run([r".venv\Scripts\ruff.exe", "format", "app/", "tests/"], capture_output=True, text=True)
    print("STDOUT:")
    print(res2.stdout)
    print(f"Exit code: {res2.returncode}")
else:
    print("Failed to upgrade dependencies.")
