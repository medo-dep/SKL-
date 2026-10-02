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

## 💻 نقلها لجهاز ثاني

الأداة ما تحتاج ذكاء اصطناعي ولا أي اشتراك، كل شي يشتغل على الجهاز نفسه.

### جهاز فيه إنترنت
1. انسخ مجلد `skl-` للجهاز الثاني (فلاشة، أو `git clone`، أو تحميل ZIP من GitHub).
2. دبل كليك على **`start-windows.bat`**.
   أول مرة يثبّت بنفسه: Python، وFFmpeg، وMicrosoft Visual C++، والمكتبات. ويسوي أيقونة **Raw to Reel** على سطح المكتب.
3. بعدها يفتح الأيقونة من سطح المكتب على طول.

### جهاز بدون إنترنت (نسخة محمولة)
1. على جهازك (اللي الأداة شغّالة فيه وفيه إنترنت): دبل كليك على **`make-portable.bat`**.
   يسوي مجلد `RawToReel-Portable` وملف `RawToReel-Portable.zip` (حوالي 1 جيجا) فيه كل شي: Python، والمكتبات، وFFmpeg، ونموذج تحويل الكلام لنص.
2. انقل ملف الـ ZIP بفلاشة للجهاز الثاني وفك الضغط.
3. دبل كليك على **`Start Raw to Reel.bat`**. ما يحتاج إنترنت ولا تثبيت.

> ميزة Pexels (B-roll والصور التلقائية) هي الوحيدة اللي تحتاج إنترنت.
