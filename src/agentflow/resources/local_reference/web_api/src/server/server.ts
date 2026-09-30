import http from 'node:http';
import { mkdirSync, readFileSync, existsSync, readdirSync, lstatSync } from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { TicketStore } from './domain.js';

function productIdentity(root:string):string {
  const rows:Array<{digest:string;executable:boolean;path:string;size:number}>=[];
  function walk(directory:string){for(const name of readdirSync(directory).sort()){
    const file=path.join(directory,name),stat=lstatSync(file);
    if(stat.isSymbolicLink())throw new Error('linked_product');
    if(stat.isDirectory())walk(file);else if(stat.isFile())rows.push({digest:'sha256:'+createHash('sha256').update(readFileSync(file)).digest('hex'),executable:(stat.mode&0o111)!==0,path:path.relative(root,file).split(path.sep).join('/'),size:stat.size});
    else throw new Error('unsafe_product');
  }}walk(root);rows.sort((a,b)=>a.path<b.path?-1:a.path>b.path?1:0);
  return 'sha256:'+createHash('sha256').update(JSON.stringify(rows)).digest('hex');
}

export function createReferenceServer(database: string, fault='') {
  const store = new TicketStore(database,fault);
  const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)),'web');
  const server = http.createServer(async (request,response) => {
    const url = new URL(request.url || '/', 'http://localhost');
    const send=(status:number,value:unknown)=>{response.writeHead(status,{'content-type':'application/json','cache-control':'no-store'});response.end(JSON.stringify(value));};
    if(url.pathname==='/health') return send(200,{status:'ok'});
    if(url.pathname==='/api/version') return send(200,{schema:'tickets-v1',source:process.env.AGENTFLOW_SOURCE_FINGERPRINT || 'reference-unbound',product_content_digest:productIdentity(path.dirname(fileURLToPath(import.meta.url)))});
    const role = request.headers.authorization?.match(/^Bearer reference\.(manager|member)$/)?.[1];
    try {
      if(url.pathname.startsWith('/api/')) {
        if(!role || !['manager','member'].includes(role)) return send(401,{error:'unauthorized'});
        let body:any={};
        if(request.method!=='GET') {
          const chunks:Buffer[]=[];let size=0;
          for await(const chunk of request){size+=chunk.length;if(size>16384){send(413,{error:'too_large'});request.destroy();return;}chunks.push(chunk);}
          body=JSON.parse(Buffer.concat(chunks).toString() || '{}');
        }
        if(url.pathname==='/api/tickets' && request.method==='GET') return send(200,{tickets:store.list()});
        if(url.pathname==='/api/tickets' && request.method==='POST') return send(201,store.create(body.title,role));
        const match=url.pathname.match(/^\/api\/tickets\/(\d+)(\/assign)?$/);
        if(match && request.method==='GET') { const t=store.get(Number(match[1]));return send(t?200:404,t || {error:'not_found'}); }
        if(match?.[2] && request.method==='POST') return send(200,store.assign(Number(match[1]),body.assignee,role));
        return send(404,{error:'not_found'});
      }
      const requested=path.resolve(webRoot,'.'+decodeURIComponent(url.pathname));
      if(requested!==webRoot && !requested.startsWith(webRoot+path.sep)) return send(403,{error:'forbidden'});
      const file=existsSync(requested) && path.extname(requested)?requested:path.join(webRoot,'index.html');
      const content=readFileSync(file);
      response.writeHead(200,{'content-type':({'.html':'text/html','.js':'text/javascript','.css':'text/css'} as Record<string,string>)[path.extname(file)] || 'application/octet-stream'});
      response.end(content);
    } catch(error:any) {
      const code=error.message==='forbidden'?403:error.message==='not_found'?404:400;
      send(code,{error:error.message || 'invalid_request'});
    }
  });
  return {server,store};
}
if(process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href){
  const directory=process.env.AGENTFLOW_TEST_DATA_DIR || path.join(tmpdir(),'agentflow-reference-data');
  mkdirSync(directory,{recursive:true});
  const {server,store}=createReferenceServer(path.join(directory,'tickets.sqlite'),process.env.AGENTFLOW_FAULT_MODE || '');
  server.listen(Number(process.env.AGENTFLOW_WEB_PORT || 8765),process.env.AGENTFLOW_LISTEN_HOST || '127.0.0.1',()=>console.log(JSON.stringify({listening:server.address()})));
  for(const sig of ['SIGTERM','SIGINT']) process.on(sig,()=>server.close(()=>{store.close();process.exit(0);}));
}
