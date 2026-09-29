let jobId=null, timer=null;
const $=id=>document.getElementById(id);
$("go").onclick=async()=>{
  $("error").textContent="";
  const url=$("url").value.trim(); if(!url)return;
  const r=await fetch("/api/jobs",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url})});
  const j=await r.json(); if(!r.ok){$("error").textContent=JSON.stringify(j);return}
  jobId=j.id;$("card").classList.remove("hidden"); poll();
};
async function selectFormat(id){
  await fetch(`/api/jobs/${jobId}/select`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({format_id:String(id)})});
  $("formats").innerHTML="";poll();
}
async function poll(){
  clearTimeout(timer); if(!jobId)return;
  const r=await fetch(`/api/jobs/${jobId}`); const j=await r.json();
  $("title").textContent=j.title||"Анализируем…"; $("stage").textContent=j.stage||j.status;
  $("bar").style.width=(j.progress||0)+"%";
  if(j.thumbnail){$("thumb").src=j.thumbnail;$("thumb").classList.remove("hidden")}else $("thumb").classList.add("hidden");
  $("error").textContent=j.error||"";
  if(j.status==="awaiting_selection"){
    $("formats").innerHTML="<h3>Выберите качество</h3>";
    for(const f of j.formats||[]){
      const b=document.createElement("button"); b.className="format";
      b.textContent=f.label||f.id;b.onclick=()=>selectFormat(f.id);$("formats").appendChild(b);
    }
  }
  if(j.status==="done"&&j.download_url){
    $("download").href=j.download_url;$("download").classList.remove("hidden");$("download").textContent="Скачать "+(j.filename||"файл");return;
  }
  if(j.status==="error")return;
  timer=setTimeout(poll,1500);
}
