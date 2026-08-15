Only 4 layers? Save and swap between 5 preset VIA .json files or manufacturer webapp export profiles globally with ctrl+shift+p. 

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

To find the correct HID settings, run profileswitchqmk.exe, hit scan, and then check https://github.com/qmk/qmk_firmware/keyboards for your keyboard or just ask claude.
After saving HID codes it will hide to the notification tray, just open it back up and add your profiles, save and away you go. 

Add to a shortcut to startup folder or create task to run at login.

I'm broke give me a buck or two if you feel like it http://buymeacoffee.com/kingkarlito
