@echo off
rem Mo "Teemo cung PDF" (cat, noi, xoay, nen, danh so trang) - chay tren may, khong dua len mang.
cd /d "%~dp0web"
start "" pythonw server.py
