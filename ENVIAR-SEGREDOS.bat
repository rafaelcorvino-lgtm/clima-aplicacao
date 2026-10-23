@echo off
rem Envia o login Zeus e a senha da pagina (de segredos.txt) para o cofre de
rem segredos do Firebase. Rode UMA vez (e de novo se trocar alguma senha).
rem E voce quem roda: o assistente nao grava senhas em servicos externos.
cd /d "%~dp0..\firebase"
setlocal EnableDelayedExpansion
for %%N in (ZEUS_EMAIL ZEUS_SENHA APP_SENHA) do (
  set "VALOR="
  for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0segredos.txt") do (
    if "%%A"=="%%N" set "VALOR=%%B"
  )
  if "!VALOR!"=="" (
    echo AVISO: %%N vazio em segredos.txt, nao enviado.
  ) else (
    <nul set /p ="!VALOR!" > "%TEMP%\segredo-%%N.txt"
    echo Enviando %%N...
    call firebase functions:secrets:set %%N --data-file "%TEMP%\segredo-%%N.txt" --force --project calendario-aerea-26140
    del "%TEMP%\segredo-%%N.txt"
  )
)
echo.
echo   Pronto. Agora rode: python aplicacao-aerea\publicar.py
echo.
pause
