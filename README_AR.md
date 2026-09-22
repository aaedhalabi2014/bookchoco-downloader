# Bookchoco Download

تطبيق خفيف لتحويل رحلة المستخدم إلى: **انسخ الرابط → اضغط → حمّل**.

## المنصات المفعّلة

النسخة الحالية تسمح بالروابط العامة من:

- Instagram
- Facebook
- X / Twitter
- TikTok
- Threads
- Reddit
- Vimeo
- YouTube / YouTube Shorts / youtu.be
- Pinterest
- Snapchat (الروابط العامة المدعومة، خصوصًا Spotlight)
- Twitch (Clips / VOD / Streams حيث يسمح المصدر)
- Dailymotion
- SoundCloud
- Streamable
- Rumble
- Bilibili
- Kick
- Bluesky
- Flickr
- 9GAG
- Odysee

المحرّك هو `yt-dlp`، لذلك الدعم الفعلي قد يتغير عندما تغيّر المنصات آلياتها. وجود المنصة في القائمة يعني أن نطاقها مسموح أمنيًا وأن `yt-dlp` يملك Extractor مناسبًا أو دعمًا معروفًا لها؛ لكنه لا يضمن أن كل رابط على المنصة سيعمل دائمًا.

> لا توجد آلية لتجاوز DRM أو المحتوى الخاص أو المحتوى الذي يحتاج تسجيل دخول. استخدم الأداة فقط للمحتوى الذي لديك حق في تنزيله أو إذن باستخدامه.

## الأمان

- Allowlist لنطاقات منصات موثوقة فقط.
- السماح بالنطاقات الفرعية يتم بحدود DNS صحيحة، لذلك `youtube.com.evil.example` لا يُقبل.
- فحص DNS ومنع loopback/private/link-local/reserved addresses.
- منع URLs التي تحتوي username/password.
- Rate Limit وحد حجم وملفات مؤقتة تُحذف تلقائيًا.
- `EXTRA_ALLOWED_HOSTS` يبقى exact-match فقط ولا يعمل كـ wildcard.

## المعمارية

- Frontend: HTML/CSS/Vanilla JS.
- Backend: FastAPI.
- Extraction: yt-dlp.
- Media processing: FFmpeg.
- Job state: SQLite.
- Storage: ملفات مؤقتة فقط.
- Render: Docker Web Service.

## تحديث yt-dlp

المنصات تغيّر آلياتها باستمرار. عند توقف منصة كانت تعمل سابقًا، حدّث إصدار `yt-dlp` ثم أعد نشر الخدمة.
