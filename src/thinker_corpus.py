#!/usr/bin/env python3
"""Build and metabolize a primary-source thinker corpus.

Acquires explicitly allow-listed public/full-view Internet Archive artifacts,
keeps raw files + checksums, normalizes OCR, emits citation-addressable GBrain
Markdown shards, and writes a deterministic corpus/source index.
"""
from __future__ import annotations

import argparse, hashlib, json, os, re, subprocess, sys, time
from pathlib import Path
from urllib.request import Request, urlopen

UA = "Mozilla/5.0 (compatible; PrimarySourceCorpusBuilder/1.0)"
ROOT = Path(os.environ.get("THINKER_CORPUS_ROOT", "/root/exports/thinker-corpora"))
BRAIN = Path(os.environ.get("BRAIN_DIR", "/root/brain"))
CORPUS_REPO = Path(os.environ.get("CORPUS_REPO", "/root/corpora"))
CONFIG = Path(os.environ.get("THINKER_CORPUS_CONFIG", "/root/.config/personal-expert/thinker-corpora"))


def fetch(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size:
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=90) as r, tmp.open("wb") as f:
        while True:
            b = r.read(1024 * 1024)
            if not b: break
            f.write(b)
    tmp.replace(dest)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""): h.update(b)
    return h.hexdigest()


def slugify(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")


def normalize_ocr(raw: str) -> str:
    raw = raw.replace("\r", "\n").replace("\x00", "")
    raw = re.sub(r"(?m)^\s*Digitized by Google\s*$", "", raw)
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n{4,}", "\n\n\n", raw)
    lines = [x.strip() for x in raw.splitlines()]
    out=[]
    for line in lines:
        if not line:
            if out and out[-1] != "": out.append("")
        else: out.append(line)
    return "\n".join(out).strip() + "\n"


def shard_text(text: str, target: int = 18000) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    shards=[]; cur=[]; n=0
    for p in paras:
        if cur and n + len(p) > target:
            shards.append("\n\n".join(cur)); cur=[]; n=0
        cur.append(p); n += len(p) + 2
    if cur: shards.append("\n\n".join(cur))
    return shards


def yaml_quote(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def load_spec(thinker: str) -> dict:
    p = CONFIG / f"{slugify(thinker)}.json"
    if not p.exists(): raise SystemExit(f"Missing corpus spec: {p}")
    return json.loads(p.read_text())


def run(cmd: list[str], env=None) -> None:
    subprocess.run(cmd, check=True, env=env)


def build(thinker: str, ingest: bool) -> dict:
    spec=load_spec(thinker); tslug=slugify(spec["thinker"]); root=ROOT/tslug
    rawdir=root/"raw"; textdir=root/"normalized"; pagedir=CORPUS_REPO
    for d in (rawdir,textdir,pagedir): d.mkdir(parents=True, exist_ok=True)
    receipts=[]
    for work in spec["works"]:
        wid=work["archive_id"]; wslug=slugify(work["title"])
        base=f"https://archive.org/download/{wid}"
        txtname=work.get("text_file", f"{wid}_djvu.txt")
        pdfname=work.get("pdf_file", f"{wid}.pdf")
        txt=rawdir/txtname; pdf=rawdir/pdfname
        fetch(f"{base}/{txtname}", txt)
        if work.get("download_pdf", True): fetch(f"{base}/{pdfname}", pdf)
        clean=normalize_ocr(txt.read_text(encoding="utf-8", errors="replace"))
        norm=textdir/f"{wslug}.txt"; norm.write_text(clean, encoding="utf-8")
        shards=shard_text(clean)
        work_pages=[]
        for idx,body in enumerate(shards,1):
            slug=f"{tslug}/works/{wslug}/part-{idx:03d}"
            md=pagedir/f"{slug}.md"
            md.parent.mkdir(parents=True, exist_ok=True)
            front=f'''---\ntitle: {yaml_quote(work['title'] + f' — Part {idx}')}\ntype: source\nauthor: {yaml_quote(spec['thinker'])}\ncorpus: {yaml_quote(tslug)}\nwork_slug: {yaml_quote(wslug)}\nwork_title: {yaml_quote(work['title'])}\npublication_year: {work.get('year','null')}\nlanguage: {yaml_quote(work.get('language','en'))}\nsource_kind: internet_archive_full_view\nsource_uri: {yaml_quote(f'https://archive.org/details/{wid}')}\nsource_record_id: {yaml_quote(wid)}\nraw_available: true\nraw_storage_path: {yaml_quote(str(pdf if pdf.exists() else txt))}\nraw_text_path: {yaml_quote(str(txt))}\nraw_sha256: {yaml_quote(sha256(pdf if pdf.exists() else txt))}\nnormalized_sha256: {yaml_quote(sha256(norm))}\npart: {idx}\nparts_total: {len(shards)}\nepistemic_layer: primary_source\nprivacy: private\n---\n\n# {work['title']} — Part {idx} of {len(shards)}\n\n> **Primary-source OCR.** Edition: {work.get('edition','unspecified')}. Scan: <https://archive.org/details/{wid}>. OCR may contain errors; verify consequential quotations against the scan.\n\n{body}\n'''
            md.write_text(front, encoding="utf-8"); work_pages.append(slug)
        receipts.append({"title":work["title"],"archive_id":wid,"year":work.get("year"),"raw_text":str(txt),"raw_pdf":str(pdf) if pdf.exists() else None,"normalized":str(norm),"normalized_sha256":sha256(norm),"characters":len(clean),"parts":len(shards),"page_slugs":work_pages})
    manifest={"schema_version":1,"thinker":spec["thinker"],"corpus_slug":tslug,"built_at":time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),"policy":"explicit full-view/public-domain allow-list","works":receipts}
    (root/"manifest.json").write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+"\n")
    index_slug=f"{tslug}/index"
    lines=["---",f"title: {yaml_quote(spec['thinker']+' Primary-Source Corpus')}","type: source",f"author: {yaml_quote(spec['thinker'])}",f"corpus: {yaml_quote(tslug)}","source_kind: primary_source_corpus","raw_available: true",f"raw_storage_path: {yaml_quote(str(root))}","epistemic_layer: corpus_index","privacy: private","---","",f"# {spec['thinker']} Primary-Source Corpus","",f"Verified pilot corpus: **{len(receipts)} works**, **{sum(x['parts'] for x in receipts)} passage shards**.","","## Works"]
    for r in receipts:
        lines += [f"- **{r['title']}** ({r.get('year')}) — Internet Archive `{r['archive_id']}` — {r['parts']} shards — SHA-256 `{r['normalized_sha256']}`"]
    lines += ["","## Metabolism contract","","This corpus participates in GBrain hybrid retrieval and embeddings. Derived synthesis must label itself `expert_commentary` or `corpus_synthesis`; primary-source shards remain immutable evidence. Consequential quotations must be checked against the linked scan.","","## Retrieval namespace","",f"`[corpora:{tslug}/works/…]`","","## Coverage boundary","",spec.get("coverage_note","This is the verified accessible pilot, not a claim of complete bibliography coverage."),""]
    idx=pagedir/f"{index_slug}.md"; idx.parent.mkdir(parents=True, exist_ok=True); idx.write_text("\n".join(lines),encoding="utf-8")
    if ingest:
        run(["git","-C",str(CORPUS_REPO),"add",f"{tslug}"])
        staged=subprocess.run(["git","-C",str(CORPUS_REPO),"diff","--cached","--quiet"]).returncode
        if staged:
            run(["git","-C",str(CORPUS_REPO),"commit","-m",f"corpus: add or update {spec['thinker']}"])
        env=os.environ.copy(); env.setdefault("GBRAIN_DISABLE_DIRECT_POOL","1")
        run(["gbrain","sync","--source","corpora","--no-pull"],env=env)
    return manifest


def metabolize(thinker: str, dream: bool) -> None:
    spec=load_spec(thinker); tslug=slugify(spec["thinker"])
    env=os.environ.copy()
    envfile=Path('/root/.hermes/.env')
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k,v=line.split('=',1); env.setdefault(k.strip(),v.strip().strip('"').strip("'"))
    env.setdefault('GBRAIN_DISABLE_DIRECT_POOL','1')
    run(["gbrain","embed","--stale"],env=env)
    if dream: run(["gbrain","dream"],env=env)
    marker=ROOT/tslug/"last-metabolized.json"
    marker.write_text(json.dumps({"at":time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),"embedded":True,"dream":dream},indent=2)+"\n")


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("thinker"); ap.add_argument("--ingest",action="store_true"); ap.add_argument("--metabolize",action="store_true"); ap.add_argument("--dream",action="store_true")
    a=ap.parse_args(); m=build(a.thinker,a.ingest)
    if a.metabolize: metabolize(a.thinker,a.dream)
    print(json.dumps({"thinker":m['thinker'],"works":len(m['works']),"parts":sum(x['parts'] for x in m['works']),"ingested":a.ingest,"metabolized":a.metabolize,"dream":a.dream}))
if __name__=='__main__': main()
