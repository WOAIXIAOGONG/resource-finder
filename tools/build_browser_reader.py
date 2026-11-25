"""Developer build only: ship the small Windows accessibility reader with the app."""
import subprocess
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
framework = Path('C:/Windows/Microsoft.NET/Framework64/v4.0.30319')
source = root/'vendor/browser-reader/BrowserReader.cs'
target = source.with_name('browser-reader.exe')
args = [str(framework/'csc.exe'), '/nologo', '/target:exe', '/optimize+', '/utf8output',
        '/out:'+str(target), '/r:System.Web.Extensions.dll', '/r:System.Windows.Forms.dll', '/r:System.Management.dll',
        '/r:'+str(framework/'WPF/UIAutomationClient.dll'),
        '/r:'+str(framework/'WPF/UIAutomationTypes.dll'), '/r:'+str(framework/'WPF/WindowsBase.dll'), str(source)]
subprocess.run(args, check=True)
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
target.with_name('manifest.json').write_text(json.dumps(dict(protocol=3,source_sha256=sha(source),binary_sha256=sha(target)),indent=2),encoding='utf-8')
print('Built browser reader:', target.stat().st_size, 'bytes')
