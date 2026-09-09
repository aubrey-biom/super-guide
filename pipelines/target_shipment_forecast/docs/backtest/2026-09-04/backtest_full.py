import pandas as pd, numpy as np
pd.set_option('display.width',220); pd.set_option('display.max_columns',30)
import pyarrow.parquet as pq
ph=pq.read_table("plan_hist_agg.parquet").to_pandas(ignore_metadata=True); act=pq.read_table("shipcast-repo/runs/2026-09-04/po_actuals_weekly.parquet").to_pandas(ignore_metadata=True)
ph['business_d']=pd.to_datetime(ph.business_d.astype(str)); ph['order_d']=pd.to_datetime(ph.order_d.astype(str))
sun=lambda s: s - pd.to_timedelta((s.dt.weekday+1)%7, unit='D')
ph['W']=sun(ph.order_d); ph=ph[ph.order_d>=ph.business_d]
f=ph.groupby(['business_d','tcin','W'],as_index=False).plan_units.sum()
f['lead']=(f.W-f.business_d).dt.days
act['wk']=pd.to_datetime(act.wk.astype(str)); a=act[['wk','tcin','act_rep']].rename(columns={'wk':'W','act_rep':'actual'})
LAST=pd.Timestamp('2026-08-30'); FIRST=pd.Timestamp('2026-05-17')
grid=f.merge(a,on=['tcin','W'],how='left'); grid['actual']=grid.actual.fillna(0)
grid=grid[(grid.W>=FIRST)&(grid.W<=LAST)]
g=grid[(grid.plan_units>0)|(grid.actual>0)].copy()
def metrics(d):
    a=d.actual.values; fc=d.plan_units.values; den=np.abs(a).sum()
    return dict(n=len(d), n_weeks=d.W.nunique(), wape=np.abs(fc-a).sum()/den if den else np.nan, bias=(fc.sum()-a.sum())/den if den else np.nan,
                exact=np.mean(np.abs(fc-a)<=0.5), within10=np.mean(np.abs(fc-a)<=0.1*a))
def bands(d):
    lr=np.log(d.actual+1)-np.log(d.plan_units+1); return np.exp(np.quantile(lr,.1)), np.exp(np.quantile(lr,.9))
rows=[]
for lead,d in g[g.lead<=56].groupby('lead'):
    m=metrics(d); m['lead_days']=lead; m['p10'],m['p90']=bands(d)
    c=d.groupby('W')[['actual','plan_units']].sum(); m['chain_wape']=np.abs(c.plan_units-c.actual).sum()/c.actual.sum()
    rows.append(m)
bl=pd.DataFrame(rows).set_index('lead_days'); print("== plan accuracy by lead days (snapshot -> order-week Sunday), scored TCIN-weeks, weeks 5/17-8/30 ==")
print(bl[['n','n_weeks','wape','bias','exact','within10','p10','p90','chain_wape']].round(3).head(15).to_string())
print(bl[['n','wape','bias','chain_wape','p10','p90']].round(3).iloc[[20,27,34,41,48,55]].to_string())
g['bucket']=np.select([g.lead<=1,g.lead<=3,g.lead<=8,g.lead<=15,g.lead<=29],['A (0-1)','B (2-3)','C (4-8)','h2 (9-15)','h3-4 (16-29)'],'h5+ (30+)')
rows=[]
for b,d in g.groupby('bucket'):
    m=metrics(d); m['bucket']=b; m['p10'],m['p90']=bands(d)
    hits=[]
    for w in d.W.unique():
        tr=d[d.W!=w]; te=d[d.W==w]; l=np.log(tr.actual+1)-np.log(tr.plan_units+1); lo,hi=np.quantile(l,[.1,.9])
        hits+=list(((te.plan_units+1)*np.exp(lo)-1<=te.actual)&(te.actual<=(te.plan_units+1)*np.exp(hi)-1))
    m['lowo_cov']=np.mean(hits); rows.append(m)
bb=pd.DataFrame(rows).set_index('bucket'); print("\n== by grade bucket ==")
print(bb[['n','n_weeks','wape','bias','exact','within10','p10','p90','lowo_cov']].round(3).to_string())
rows=[]
for h in range(1,9):
    parts=[]
    for W in sorted(g.W.unique()):
        o=W-pd.Timedelta(days=7*(h-1)+1); bd=f[f.business_d<=o].business_d.max()
        if pd.isna(bd): continue
        parts.append(grid[(grid.W==W)&(grid.business_d==bd)])
    d=pd.concat(parts); d=d[(d.plan_units>0)|(d.actual>0)]
    m=metrics(d); c=d.groupby('W')[['actual','plan_units']].sum(); m['chain_wape']=np.abs(c.plan_units-c.actual).sum()/c.actual.sum(); m['p10'],m['p90']=bands(d); m['h']=h; rows.append(m)
bh=pd.DataFrame(rows).set_index('h'); print("\n== by horizon (freshest snapshot as of the Saturday before origin week) ==")
print(bh[['n','n_weeks','wape','bias','chain_wape','exact','within10','p10','p90']].round(3).to_string())
rows=[]
for M in [pd.Timestamp('2026-06-01'),pd.Timestamp('2026-07-01'),pd.Timestamp('2026-08-01')]:
    o=M-pd.Timedelta(days=1); bd=f[f.business_d<=o].business_d.max()
    weeks=[w for w in g.W.unique() if (w>=M)&(w<M+pd.DateOffset(months=1))]
    d=grid[(grid.business_d==bd)&(grid.W.isin(weeks))].groupby('tcin',as_index=False)[['plan_units','actual']].sum()
    d=d[(d.plan_units>0)|(d.actual>0)]; a_=d.actual.values; fc=d.plan_units.values; lr=np.log(a_+1)-np.log(fc+1)
    rows.append(dict(month=M.strftime('%b-%y'), snapshot=bd.date(), n_tcin=len(d), wape=np.abs(fc-a_).sum()/a_.sum(), bias=(fc.sum()-a_.sum())/a_.sum(), chain_actual=int(a_.sum()), chain_plan=int(fc.sum()),
                     within25=np.mean(np.abs(fc-a_)<=0.25*a_), p10=np.exp(np.quantile(lr,.1)), p90=np.exp(np.quantile(lr,.9))))
bm=pd.DataFrame(rows); print("\n== monthly: plan month total at month start vs actual month PO units ==")
print(bm.round(3).to_string(index=False))
bl.to_csv('backtest_by_lead.csv'); bb.to_csv('backtest_by_bucket.csv'); bh.to_csv('backtest_by_horizon.csv'); bm.to_csv('backtest_monthly.csv',index=False)
