HTML = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MrBlack AI</title>

<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:Inter,system-ui;}

body{
background:#f9fafb;
color:#111827;
height:100vh;
display:flex;
flex-direction:column;
}

.topbar{
height:60px;
background:white;
display:flex;
align-items:center;
justify-content:space-between;
padding:0 25px;
box-shadow:0 2px 10px rgba(0,0,0,0.05);
}

.brand{
font-size:20px;
font-weight:600;
background:linear-gradient(90deg,#2563eb,#06b6d4);
-webkit-background-clip:text;
-webkit-text-fill-color:transparent;
}

.status{
display:flex;
align-items:center;
gap:8px;
font-size:14px;
color:#6b7280;
}

.dot{
width:10px;
height:10px;
border-radius:50%;
background:#22c55e;
}

.container{
flex:1;
display:flex;
padding:20px;
gap:20px;
}

.left{
width:60%;
display:flex;
flex-direction:column;
gap:20px;
}

.right{
width:40%;
display:flex;
flex-direction:column;
justify-content:space-between;
padding-bottom:60px; /* 👈 bottom breathing space */
}

.card{
background:white;
padding:20px;
border-radius:20px;
box-shadow:0 10px 25px rgba(0,0,0,0.05);
}

.balance{
font-size:34px;
font-weight:700;
background:linear-gradient(90deg,#16a34a,#22c55e);
-webkit-background-clip:text;
-webkit-text-fill-color:transparent;
margin-top:10px;
}

.btn{
width:100%;
padding:14px;
border:none;
border-radius:14px;
font-size:15px;
cursor:pointer;
margin-top:12px;
font-weight:600;
transition:0.2s;
}

.start{
background:linear-gradient(90deg,#2563eb,#06b6d4);
color:white;
}

.start:hover{opacity:0.9;}

.stop{
background:#ef4444;
color:white;
}

.logs{
background:#f3f4f6;
padding:15px;
border-radius:14px;
height:180px;
overflow-y:auto;
font-size:13px;
color:#374151;
margin-top:10px;
}

.chatbox{
height:calc(100vh - 240px);
background:white;
border-radius:20px;
box-shadow:0 10px 25px rgba(0,0,0,0.05);
padding:15px;
overflow-y:auto;
display:flex;
flex-direction:column;
gap:12px;
}

.msg{
max-width:75%;
padding:12px 16px;
border-radius:18px;
font-size:14px;
}

.user{
align-self:flex-end;
background:linear-gradient(90deg,#2563eb,#06b6d4);
color:white;
}

.bot{
align-self:flex-start;
background:#f3f4f6;
}

.inputbar{
display:flex;
margin-top:20px;
}

.inputbar input{
flex:1;
padding:14px;
border-radius:30px;
border:1px solid #e5e7eb;
outline:none;
font-size:14px;
background:white;
}

.inputbar button{
width:50px;
margin-left:10px;
border-radius:50%;
border:none;
background:linear-gradient(90deg,#2563eb,#06b6d4);
color:white;
font-size:18px;
cursor:pointer;
}
</style>
</head>

<body>

<div class="topbar">
<div class="brand">MRBLACK AI</div>
<div class="status">
<div class="dot"></div>
Bot Active
</div>
</div>

<div class="container">

<div class="left">

<div class="card">
<div>Wallet Balance</div>
<div class="balance" id="balance">2.45 SOL</div>
</div>

<div class="card">
<button class="btn start" onclick="cmd('start bot')">Start Trading Bot</button>
<button class="btn stop" onclick="cmd('stop bot')">Stop Trading Bot</button>

<div class="logs">
Bot initialized...<br>
Waiting for signal...
</div>
</div>

</div>

<div class="right">

<div class="chatbox" id="chat"></div>

<div class="inputbar">
<input id="input" placeholder="Ask AI something...">
<button onclick="sendMsg()">➤</button>
</div>

</div>

</div>

<script>
const chat=document.getElementById('chat');
const input=document.getElementById('input');

function addMsg(text,user){
const div=document.createElement('div');
div.className='msg '+(user?'user':'bot');
div.innerHTML=text;
chat.appendChild(div);
chat.scrollTop=chat.scrollHeight;
}

async function sendMsg(){
const msg=input.value.trim();
if(!msg) return;
addMsg(msg,true);
input.value='';
addMsg("Thinking...",false);

const res=await fetch('/chat',{
method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({message:msg})
});
const data=await res.json();
chat.lastChild.remove();
addMsg(data.reply,false);
}

function cmd(text){
input.value=text;
sendMsg();
}

window.onload=()=>addMsg("Welcome bhai 🤍 System ready.",false);
</script>

</body>
</html>
"""
