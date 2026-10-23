@echo off
rem Envia o login Zeus e a senha da pagina (de segredos.txt) para os segredos do
rem repositorio no GitHub, que o robo (GitHub Actions) usa. Rode UMA vez (e de
rem novo se trocar alguma senha). E voce quem roda: o assistente nao grava
rem senhas em servicos externos.
cd /d "%~dp0"
setlocal EnableDelayedExpansion
set REPO=rafaelcorvino-lgtm/clima-aplicacao
gh auth status -h github.com >nul 2>nul
if errorlevel 1 (
  echo.
  echo   O gh ainda nao esta logado. Vai abrir o navegador: copie o codigo mostrado,
  echo   entre no GitHub e clique em Authorize.
  echo.
  gh auth login -h github.com -s repo,workflow -w
)
echo.
echo   Enviando os segredos do repositorio...
echo.
for %%N in (ZEUS_EMAIL ZEUS_SENHA APP_SENHA) do (
  set "VALOR="
  for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0segredos.txt") do (
    if "%%A"=="%%N" set "VALOR=%%B"
  )
  if "!VALOR!"=="" (
    echo AVISO: %%N vazio em segredos.txt, nao enviado.
  ) else (
    echo Enviando %%N...
    <nul set /p ="!VALOR!" | gh secret set %%N --repo %REPO%
  )
)
echo.
echo   Conferindo:
gh secret list --repo %REPO%
echo.
echo   Pronto. Avise o assistente para disparar a primeira execucao.
echo.
pause
