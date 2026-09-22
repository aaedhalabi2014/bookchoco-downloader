# Bookchoco Download

تطبيق PWA خفيف لتحويل رحلة المستخدم إلى: **انسخ الرابط → اضغط → حمّل**.

## ما الذي يدعمه افتراضيًا؟

Instagram، Facebook، X/Twitter، TikTok، Threads، Reddit، Vimeo — للروابط العامة التي يستطيع `yt-dlp` استخراجها دون حسابات أو Cookies خاصة.

> لا توجد آلية لتجاوز DRM أو المحتوى الخاص أو المحتوى الذي يحتاج تسجيل دخول. استخدم الأداة فقط للمحتوى الذي لديك حق في تنزيله أو إذن باستخدامه.

## المعمارية

- Frontend: HTML/CSS/Vanilla JS؛ بدون Framework ثقيل.
- PWA: Manifest + Service Worker + أيقونات تثبيت + Web Share Target على الأنظمة الداعمة.
- Backend: FastAPI.
- Extraction: yt-dlp.
- Media processing: FFmpeg.
- Job state: SQLite محلي.
- Storage: ملفات مؤقتة فقط؛ الملفات مؤقتة؛ بعد بدء التسليم تبقى نافذة قصيرة لدعم Range requests ثم تُحذف تلقائيًا، كما تنظف العمليات القديمة تلقائيًا.
- Security: Allowlist للنطاقات، فحص DNS ضد العناوين الخاصة/المحلية، حد حجم، Rate Limit، CSP، no-store، تشغيل Docker كمستخدم غير root مع capabilities محذوفة.

## تشغيل محلي سريع

يتطلب Python 3.11+ وFFmpeg.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

ثم افتح `http://127.0.0.1:8000`.

ملاحظة: قراءة Clipboard من المتصفح تحتاج HTTPS في الإنتاج (localhost مستثنى عادةً كبيئة آمنة).

## تشغيل Production بواسطة Docker

```bash
cp .env.example .env
# عدّل PUBLIC_BASE_URL والإعدادات إن لزم
docker compose up -d --build
```

الخدمة ستستمع محليًا على `127.0.0.1:8000`. ضع Nginx أو Caddy أمامها مع HTTPS. ملف `nginx.conf.example` مرفق كنقطة بداية.

## إعداد الدومين

1. أنشئ سجل DNS من نوع A للدومين الفرعي، مثل `download.bookchoco.online`، إلى IP الخادم.
2. فعّل HTTPS (Let's Encrypt/Certbot أو Cloudflare Origin).
3. مرر الترافيك إلى `127.0.0.1:8000`.
4. تأكد أن مسار `/healthz` يعيد `{ "ok": true }`.

## متغيرات مهمة

- `MAX_VIDEO_MB=750`: الحد الأعلى للملف.
- `DOWNLOAD_MAX_HEIGHT=1080`: حد الدقة لتقليل الضغط على السيرفر.
- `MAX_CONCURRENT_DOWNLOADS=3`: أقصى عمليات تجهيز متزامنة.
- `RATE_LIMIT_REQUESTS=12`: عدد طلبات الإنشاء ضمن النافذة.
- `JOB_TTL_MINUTES=60`: حذف العمليات/الملفات القديمة.
- `EXTRA_ALLOWED_HOSTS=`: إضافة نطاقات موثوقة يدويًا. لا تستخدم wildcard عشوائيًا.

## تحديث yt-dlp

المنصات تغيّر آلياتها باستمرار. عند توقف منصة كانت تعمل سابقًا، حدّث إصدار `yt-dlp` في `requirements.txt` ثم أعد بناء الحاوية.

## ملاحظات Scaling

الإصدار الحالي مصمم لخادم واحد وUvicorn worker واحد. SQLite والحجم المؤقت مناسبان لهذا السيناريو. عند زيادة الحمل بشكل كبير، انقل jobs إلى Redis/Celery أو RQ، والملفات المؤقتة إلى object storage أو worker-local storage مع signed delivery، وأضف rate limiting مركزيًا على Redis/edge.

## ملاحظة أمان حول الـ Reverse Proxy

ملف Docker يشغّل Uvicorn مع الثقة بـ forwarded headers لأن `docker-compose.yml` يربط المنفذ على `127.0.0.1` فقط، أي أن الوصول الخارجي يجب أن يمر عبر Nginx/Caddy. **لا تغيّر الربط إلى `0.0.0.0:8000` مع إبقاء الثقة المفتوحة بالـforwarded headers** إلا إذا ضبطت قائمة البروكسيات الموثوقة بشكل صريح.
