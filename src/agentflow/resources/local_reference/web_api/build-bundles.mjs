import {cpSync,mkdirSync,rmSync,writeFileSync} from 'node:fs';
rmSync('bundle',{recursive:true,force:true});mkdirSync('bundle/product',{recursive:true});mkdirSync('bundle/tests',{recursive:true});mkdirSync('reports',{recursive:true});
cpSync('dist','bundle/product',{recursive:true});
cpSync('tests','bundle/tests',{recursive:true});
cpSync('node_modules','bundle/tests/node_modules',{recursive:true,dereference:true,filter:p=>!p.split(/[\\/]/).includes('.bin')});
writeFileSync('bundle/product/package.json',JSON.stringify({type:'module'}));
writeFileSync('bundle/tests/package.json',JSON.stringify({type:'module'}));
