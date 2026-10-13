"""Run the local checks available without cloud or Home Assistant access."""
import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
version_text = (ROOT / 'config.yaml').read_text(encoding='utf-8')
match = re.search(r'^version:\s*(\S+)\s*$', version_text, re.M)
if not match:
    raise SystemExit('config.yaml is missing a version')
version = match.group(1)
dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
if f'io.hass.version="{version}"' not in dockerfile:
    raise SystemExit('Dockerfile and config.yaml versions differ')
for source in [*ROOT.joinpath('app').glob('*.py'), *ROOT.joinpath('tests').glob('*.py')]:
    ast.parse(source.read_text(encoding='utf-8'), filename=str(source), feature_version=(3, 12))
subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], cwd=ROOT, check=True)
print(f'SolarSynkV4 {version}: local checks passed')
