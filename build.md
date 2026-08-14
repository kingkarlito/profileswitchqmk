git clone profileswitchqmk
cd profileswitchqmk
uv venv
.venv\Scripts\activate
uv pip install -r .\requirements.txt
# skip this next line if you don't want to build an .exe with pyinstaller
uv pip install pyinstaller
./dist/profileswitchqmk/profileswitchqmk.exe