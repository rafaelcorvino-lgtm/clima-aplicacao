@echo off
rem Faz o Clima de Aplicacao abrir sozinho (minimizado) toda vez que o Windows entrar.
rem Rode uma vez. Para desfazer, apague o arquivo ClimaDeAplicacao.bat da pasta Iniciar
rem (Win+R, digite shell:startup).
set DESTINO=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ClimaDeAplicacao.bat
(
  echo @echo off
  echo start "Clima de Aplicacao" /min cmd /c ""%~dp0INICIAR.bat""
) > "%DESTINO%"
echo.
echo   Pronto: o Clima de Aplicacao vai iniciar junto com o Windows.
echo   Arquivo criado em:
echo   %DESTINO%
echo.
pause
