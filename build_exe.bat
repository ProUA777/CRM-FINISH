@echo off
REM Збірка Оснастка-Маркет у один .exe-файл на Windows.
REM Запускати ЛИШЕ на Windows (PyInstaller не вміє крос-компіляцію
REM з Linux/macOS у Windows-exe — це обмеження самого PyInstaller,
REM не цього проєкту).
REM
REM Використання:
REM   1. Встанови Python 3.11+ з python.org (позначити "Add to PATH")
REM   2. Відкрий цю теку в командному рядку (cmd) і виконай:
REM        build_exe.bat
REM   3. Готовий файл з'явиться в dist\OsnastkaMarket.exe

echo === Оснастка-Маркет: збірка .exe ===

python -m venv build_venv
call build_venv\Scripts\activate.bat

pip install --upgrade pip
pip install -r requirements.txt
pip install pyinstaller pystray pillow

python make_icon.py

pyinstaller --onefile --noconsole --name OsnastkaMarket --icon=icon.ico ^
    --hidden-import=reportlab.graphics.barcode ^
    --hidden-import=openpyxl.cell._writer ^
    --hidden-import=waitress ^
    --hidden-import=features_ext ^
    --hidden-import=features_templates ^
    --hidden-import=tkinter ^
    --hidden-import=tkinter.messagebox ^
    --hidden-import=segno ^
    --hidden-import=ezdxf ^
    --hidden-import=ezdxf.addons.drawing ^
    --hidden-import=ezdxf.addons.drawing.matplotlib ^
    --hidden-import=matplotlib.backends.backend_agg ^
    --hidden-import=fitz ^
    --collect-all reportlab ^
    --collect-all ezdxf ^
    --collect-all matplotlib ^
    --collect-all pymupdf ^
    launcher.py

echo.
echo === Готово: dist\OsnastkaMarket.exe ===
echo Далі можна обгорнути в повноцінний інсталятор через installer.iss (Inno Setup).
pause
