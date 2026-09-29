const $ = s => document.querySelector(s);
let currentUrl = "";
let pollTimer = null;

function show(el){ el.classList.remove("hidden"); }
function hide(el){ el.classList.add("hidden"); }
function fmtDuration(s){
  if(!s) return "";
  const h=Math.floor(s/3600), m=Math.floor((s%3600)/60), sec=Math.floor(s%60);
  return h ? `${h}:${String(m).padStart(2,"0")}:${String(sec).padStart(2,"0")}` : `${m}:${String(sec).padStart(2,"0")}`;
}
function fmtBytes(n){
  if(n == null) return "";
  const u=["Б","КБ","МБ","ГБ","ТБ"]; let i=0, v=n;
  while(v>=1024 && i<u.length-1){v/=1024;i++}
  return `${v.toFixed(i?1:0)} ${u[i]}`;
}
function err(text){ $("#topError").textContent=text; show($("#topError")); }

$("#searchForm").addEventListener("submit", async e=>{
  e.preventDefault();
  hide($("#topError")); hide($("#videoCard")); hide($("#doneCard")); hide($("#progressCard"));
  currentUrl=$("#urlInput").value.trim();
  $("#analyzeBtn").disabled=true; $("#analyzeBtn").textContent="Анализ…";
  try{
    const r=await fetch("/api/analyze",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url:currentUrl})});
    const d=await r.json();
    if(!r.ok) throw new Error(d.detail || "Ошибка анализа");
    $("#title").textContent=d.title;
    $("#thumb").src=d.thumbnail || "";
    $("#subline").textContent=[d.uploader,fmtDuration(d.duration),d.max_height?`до ${d.max_height}p`:""].filter(Boolean).join(" · ");
    const q=$("#qualities"); q.innerHTML="";
    d.available.forEach(height=>{
      const b=document.createElement("button"); b.className="quality";
      b.textContent=height===2160?"2160p · 4K":height===1440?"1440p · 2K":`${height}p`;
      b.onclick=()=>startDownload(height); q.appendChild(b);
    });
    show($("#videoCard"));
    $("#videoCard").scrollIntoView({behavior:"smooth",block:"center"});
  }catch(e){err(e.message)}
  finally{$("#analyzeBtn").disabled=false;$("#analyzeBtn").textContent="Найти видео"}
});

async function startDownload(height){
  hide($("#doneCard")); show($("#progressCard"));
  $("#stageLabel").textContent="ОЧЕРЕДЬ"; $("#progressTitle").textContent=`Подготавливаем ${height}p`;
  $("#barFill").style.width="0%"; $("#percent").textContent="0%"; $("#bytes").textContent="";
  $("#message").textContent="Создаём задачу…";
  $("#progressCard").scrollIntoView({behavior:"smooth",block:"center"});
  try{
    const r=await fetch("/api/download",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url:currentUrl,height})});
    const d=await r.json(); if(!r.ok) throw new Error(d.detail||"Не удалось создать задачу");
    poll(d.job_id);
  }catch(e){err(e.message)}
}
async function poll(id){
  clearTimeout(pollTimer);
  try{
    const r=await fetch(`/api/jobs/${id}`,{cache:"no-store"}); const d=await r.json();
    if(!r.ok) throw new Error(d.detail||"Ошибка статуса");
    const labels={queued:"ОЧЕРЕДЬ",download:"YOUTUBE",merge:"FFMPEG",upload:"CLOUDFLARE R2",done:"ГОТОВО",error:"ОШИБКА"};
    $("#stageLabel").textContent=labels[d.stage]||"ОБРАБОТКА";
    const p=d.progress==null?0:d.progress;
    $("#barFill").style.width=`${p}%`; $("#percent").textContent=d.progress==null?"…":`${p.toFixed(1)}%`;
    $("#bytes").textContent=d.total_bytes?`${fmtBytes(d.downloaded_bytes||0)} / ${fmtBytes(d.total_bytes)}`:"";
    $("#message").textContent=d.message||"";
    if(d.status==="done"){
      hide($("#progressCard")); show($("#doneCard"));
      $("#doneInfo").textContent=`${d.height}p · ${fmtBytes(d.file_size)} · ссылка временная`;
      $("#downloadLink").href=d.download_url;
      $("#doneCard").scrollIntoView({behavior:"smooth",block:"center"});
      return;
    }
    if(d.status==="error") throw new Error(d.message);
    pollTimer=setTimeout(()=>poll(id),1000);
  }catch(e){
    $("#stageLabel").textContent="ОШИБКА"; $("#message").textContent=e.message; $("#barFill").style.width="0%";
  }
}
