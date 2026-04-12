@echo off
cd /d "C:\Users\user\PycharmProjects\CryptoInvestmentSignal"
set PYTHONUTF8=1
python crypto_signal.py >> logs\crypto_signal.log 2>&1
