# Windows: one-shot demo launcher
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install doipclient udsoncan streamlit anthropic
Start-Process -NoNewWindow .\.venv\Scripts\python.exe "doip_simulator.py"
Start-Sleep -Seconds 2
.\.venv\Scripts\streamlit.exe run app.py
