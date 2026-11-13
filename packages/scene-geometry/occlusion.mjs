// Order convex projected faces by depth in their actual screen overlap.
// Whole-face average depth is insufficient for long boards behind short fronts.
// This is a display calculation, never manufacturing or collision evidence.
export function orderConvexFaces(faces) {
  const epsilon = 1e-7;
  const cross = (a, b, p) => (b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]);
  const area = points => points.reduce((sum, p, i) => {
    const q = points[(i+1)%points.length];
    return sum + p[0]*q[1]-q[0]*p[1];
  }, 0);
  function plane(points) {
    const a=points[0];
    for(let i=1;i<points.length-1;i++) {
      const b=points[i],c=points[i+1],d=cross(a,b,c);
      if(Math.abs(d)<epsilon) continue;
      const x=((b[2]-a[2])*(c[1]-a[1])-(c[2]-a[2])*(b[1]-a[1]))/d;
      const y=((b[0]-a[0])*(c[2]-a[2])-(c[0]-a[0])*(b[2]-a[2]))/d;
      return p => a[2]+x*(p[0]-a[0])+y*(p[1]-a[1]);
    }
    return null;
  }
  function intersection(subject, clip) {
    let output=subject.map(p=>p.slice(0,2));
    const sign=area(clip)>0?1:-1;
    for(let i=0;i<clip.length && output.length;i++) {
      const a=clip[i],b=clip[(i+1)%clip.length],input=output;
      output=[];
      for(let j=0;j<input.length;j++) {
        const p=input[j],q=input[(j+1)%input.length];
        const dp=sign*cross(a,b,p),dq=sign*cross(a,b,q);
        if(dp>=-epsilon) output.push(p);
        if((dp>epsilon && dq<-epsilon)||(dp<-epsilon && dq>epsilon)) {
          const t=dp/(dp-dq);
          output.push([p[0]+t*(q[0]-p[0]),p[1]+t*(q[1]-p[1])]);
        }
      }
    }
    return output;
  }
  const data=faces.map(f=>({face:f,depth:plane(f.points),bounds:[
    Math.min(...f.points.map(p=>p[0])),Math.max(...f.points.map(p=>p[0])),
    Math.min(...f.points.map(p=>p[1])),Math.max(...f.points.map(p=>p[1]))],next:[],incoming:0}));
  for(let i=0;i<data.length;i++)for(let j=i+1;j<data.length;j++) {
    const a=data[i],b=data[j],x=a.bounds,y=b.bounds;
    if(!a.depth||!b.depth||x[0]>=y[1]||y[0]>=x[1]||x[2]>=y[3]||y[2]>=x[3])continue;
    const overlap=intersection(a.face.points,b.face.points);
    if(overlap.length<3||Math.abs(area(overlap))<epsilon)continue;
    const center=[0,1].map(k=>overlap.reduce((sum,p)=>sum+p[k],0)/overlap.length);
    const delta=a.depth(center)-b.depth(center);
    if(Math.abs(delta)<epsilon)continue;
    const far=delta>0?i:j,near=delta>0?j:i;
    data[far].next.push(near);data[near].incoming++;
  }
  const ready=data.map((_,i)=>i).filter(i=>data[i].incoming===0),ordered=[],seen=new Set();
  while(ready.length) {
    const i=ready.pop();seen.add(i);ordered.push(data[i].face);
    for(const n of data[i].next)if(--data[n].incoming===0)ready.push(n);
  }
  const unresolved=data.length-ordered.length;
  // Intersecting/cyclic user-authored previews remain viewable, but are labelled incomplete.
  ordered.push(...data.filter((_,i)=>!seen.has(i)).map(d=>d.face));
  return {faces:ordered,unresolved};
}
