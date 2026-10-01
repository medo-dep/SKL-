# SKL — أدوات ومهارات Claude Code

## ✳ [Raw to Reel](raw-to-reel/README.md)
مونتاج الفيديو الخام إلى ريل جاهز بضغطة واحدة: قص الصمت والكلمات الزائدة والإعادات، ترجمة متحركة، زوم، صوت استوديو، موسيقى، وTimeline لـ DaVinci Resolve.

```bash
python3 raw-to-reel/server.py   # تنفتح الصفحة في كروم على http://127.0.0.1:4680
```
أو من Claude Code داخل هذا المجلد: `/raw-to-reel`

## 🪟 التثبيت على ويندوز (مرة وحدة)
افتح **PowerShell** وانسخ هذي الأوامر:
```powershell
winget install Python.Python.3.12
winget install Gyan.FFmpeg
winget install Git.Git
```
سكّر PowerShell وافتحه من جديد، وبعدين:
```powershell
cd $HOME\Desktop
git clone https://github.com/medo-dep/skl-
```

## ▶️ التشغيل
افتح مجلد `skl-` على سطح المكتب واضغط دبل كليك على **`start-windows.bat`**. أول مرة يثبّت Whisper بروحه (ياخذ دقايق)، وبعدها تنفتح الصفحة في كروم.
لا تسكّر النافذة السوداء طول ما أنت تستخدم الأداة.
