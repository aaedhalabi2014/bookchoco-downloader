const $=id=>document.getElementById(id);
const API_BASE=location.hostname==="download.bookchoco.online"?"https://api-download.bookchoco.online":"";
const pasteButton=$("pasteButton"),idleCopy=$("idleCopy"),manualForm=$("manualForm"),urlInput=$("urlInput"),progressPanel=$("progressPanel"),readyPanel=$("readyPanel"),errorPanel=$("errorPanel"),platformBadge=$("platformBadge"),statusText=$("statusText"),progressBar=$("progressBar"),progressValue=$("progressValue"),downloadButton=$("downloadButton"),readyTitle=$("readyTitle"),readyPlatform=$("readyPlatform"),installBtn=$("installBtn");
let timer=null,deferred=null,wakeTimer=null;
const copy={queued:"أرسلنا الرابط…",preparing:"نتحقق من الفيديو…",downloading:"جاري تجهيز الفيديو…",processing:"اللمسات الأخيرة…"};
function hide(){manualForm.hidden=progressPanel.hidden=readyPanel.hidden=errorPanel.hidden=true}
function clearTimers(){if(timer)clearTimeout(timer);if(wakeTimer)clearTimeout(wakeTimer);timer=wakeTimer=null}
function reset(){clearTimers();hide();idleCopy.hidden=false;urlInput.value="";progressBar.style.width="0%";progressValue.textContent="0%"}
function manual(msg=""){hide();idleCopy.hidden=true;manualForm.hidden=false;urlInput.placeholder=msg||"https://...";urlInput.focus()}
function error(msg){clearTimers();hide();idleCopy.hidden=true;errorPanel.hidden=false;$("errorText").textContent=msg||"تعذر تجهيز الفيديو."}
function progress(j){hide();idleCopy.hidden=true;progressPanel.hidden=false;const v=Math.max(0,Math.min(100,Number(j.progress||0)));platformBadge.textContent=String(j.platform||"VIDEO").toUpperCase();statusText.textContent=copy[j.status]||"جاري التجهيز…";progressBar.style.width=v+"%";progressValue.textContent=v+"%"}
function ready(j){clearTimers();hide();idleCopy.hidden=true;readyPanel.hidden=false;readyTitle.textContent=j.title||"الفيديو جاهز";readyPlatform.textContent=(j.platform||"Video")+" · الملف جاهز للتحميل";downloadButton.href=API_BASE+j.download_url}
async function api(path,opt={}){const r=await fetch(API_BASE+path,{...opt,headers:{"Content-Type":"application/json",...(opt.headers||{})}});let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.error||"حدث خطأ أثناء الاتصال بالخادم.");return d}
function extract(v){const m=String(v||"").trim().match(/https?:\/\/[^\s<>"']+/i);return m?m[0].replace(/[),.;!?،؛]+$/u,""):""}
async function create(v){
  const u=extract(v);if(!u)return manual("ألصق رابطًا كاملًا يبدأ بـ https://");
  progress({status:"queued",progress:1,platform:"VIDEO"});
  wakeTimer=setTimeout(()=>{if(!progressPanel.hidden){statusText.textContent="جاري تشغيل محرك التحميل…";progressValue.textContent="قد يستغرق التشغيل الأول أقل من دقيقة";}},3500);
  try{const j=await api("/api/jobs",{method:"POST",body:JSON.stringify({url:u})});if(wakeTimer)clearTimeout(wakeTimer);wakeTimer=null;progress(j);poll(j.id)}
  catch(e){error(e.message)}
}
async function poll(id){try{const j=await api("/api/jobs/"+id);if(j.status==="ready"||j.status==="served")return ready(j);if(j.status==="error")return error(j.error);progress(j);timer=setTimeout(()=>poll(id),1100)}catch(e){error(e.message)}}
pasteButton.onclick=async()=>{if(!navigator.clipboard?.readText)return manual();try{const t=await navigator.clipboard.readText();t.trim()?create(t):manual("الحافظة فارغة — الصق الرابط هنا")}catch{manual("اضغط مطولًا والصق الرابط هنا")}};
manualForm.onsubmit=e=>{e.preventDefault();create(urlInput.value)};
$("againButton").onclick=reset;$("retryButton").onclick=reset;
window.addEventListener("beforeinstallprompt",e=>{e.preventDefault();deferred=e;installBtn.hidden=false});
installBtn.onclick=async()=>{if(!deferred)return;deferred.prompt();await deferred.userChoice;deferred=null;installBtn.hidden=true};
const q=new URLSearchParams(location.search),shared=q.get("url")||q.get("text")||"";if(shared&&extract(shared)){history.replaceState({},"",location.pathname);create(shared)}
if("serviceWorker"in navigator)addEventListener("load",()=>navigator.serviceWorker.register("/sw.js").catch(()=>{}));