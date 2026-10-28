# 로컬 실행 도구

노트북에서 사용하는 `cloudflared.exe`를 이 폴더에 둡니다.
실행 파일은 Git과 배포 이미지에 포함하지 않습니다.

프로젝트 루트에서 실행합니다.

```powershell
.\tools\cloudflared.exe tunnel --url http://127.0.0.1:8000
```

학교 서버의 공개 주소를 카카오 웹훅에 연결한 후에는 이 로컬 터널이 필요하지 않습니다.
