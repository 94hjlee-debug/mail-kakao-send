$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Write-Host "카카오 직원 셀프 등록 서버를 시작합니다."
Write-Host "종료하려면 Ctrl+C 를 누르세요."
python .\kakao_register_server.py
