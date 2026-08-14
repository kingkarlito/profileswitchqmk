Follow the steps in build.md to install:
powershell
```

git clone https://github.com/kingkarlito/profileswitchqmk.git
cd profileswitchqmk
uv venv
.venv\Scripts\activate
uv pip install -r .\requirements.txt
# skip this next line if you don't want to build an .exe with pyinstaller
uv pip install pyinstaller
.\dist\profileswitchqmk\profileswitchqmk.exe

```
reverse the \ to / if your in nix or wsl

I'm broke give me a buck or two if you feel like it http://buymeacoffee.com/kingkarlito
