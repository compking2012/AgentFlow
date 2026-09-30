import React, {useEffect,useState} from 'react';
import {createRoot} from 'react-dom/client';
import './style.css';
type Ticket={id:number;title:string;owner:string;assignee:string|null;status:string};
function App(){
  const [role,setRole]=useState(sessionStorage.getItem('role') || 'manager');
  const [signed,setSigned]=useState(sessionStorage.getItem('signed')==='yes');
  const [title,setTitle]=useState('');const [tickets,setTickets]=useState<Ticket[]>([]);const [error,setError]=useState('');
  async function request(path:string,body?:object){
    const response=await fetch('/api'+path,{method:body?'POST':'GET',headers:{authorization:'Bearer reference.'+role,'content-type':'application/json'},body:body?JSON.stringify(body):undefined});
    const data=await response.json();if(!response.ok)throw new Error(data.error);return data;
  }
  async function refresh(){try{setTickets((await request('/tickets')).tickets);setError('');}catch(e:any){setError(e.message);}}
  useEffect(()=>{if(signed)void refresh();},[signed,role]);
  return <main><h1>AgentFlow Tickets</h1><p>Reference application · real API and native-client shared data</p>
    <label>Role<select aria-label="Role" value={role} onChange={e=>{setRole(e.target.value);sessionStorage.setItem('role',e.target.value);}}><option>manager</option><option>member</option></select></label>
    <button onClick={()=>{sessionStorage.setItem('signed','yes');setSigned(true);}}>Sign in</button>
    {signed && <><form onSubmit={async e=>{e.preventDefault();try{await request('/tickets',{title});setTitle('');await refresh();}catch(e:any){setError(e.message);}}}>
      <label>Ticket title<input aria-label="Ticket title" value={title} onChange={e=>setTitle(e.target.value)}/></label><button>Create ticket</button></form>
      <button onClick={refresh}>Refresh</button><ul aria-label="Tickets">{tickets.map(t=><li key={t.id}><strong>{t.title}</strong><span> · {t.status} · assigned: {t.assignee || 'none'}</span><button aria-label={'Assign '+t.title} onClick={async()=>{try{await request('/tickets/'+t.id+'/assign',{assignee:'member'});await refresh();}catch(e:any){setError(e.message);}}}>Assign to member</button></li>)}</ul></>}
    <p role="alert">{error}</p></main>;
}
createRoot(document.getElementById('root')!).render(<App/>);
