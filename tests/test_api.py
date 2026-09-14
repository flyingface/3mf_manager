# -*- coding: utf-8 -*-
"""API 端到端测试：上传/解析/分类/归档/查询/tag/附件/hash重复。"""
import os, json, urllib.request, urllib.parse


# client fixture 在 conftest.py 定义（隔离环境测试服务器）


def fetch(base, path, data=None, files=None):
    """支持 JSON 与 multipart。"""
    if files:  # multipart
        boundary = "----testboundary"
        parts = []
        for name, fn, content in files:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{fn}"\r\n\r\n'.encode())
            parts.append(content + b"\r\n")  # 真实 multipart：内容后有 \r\n 再接 boundary
        if data:
            for k, v in data.items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(base + path, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    elif data is not None:
        req = urllib.request.Request(base + path, data=json.dumps(data).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
    else:
        req = urllib.request.Request(base + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": json.loads(e.read().decode()).get("error", "http" + str(e.code))}


def _make_3mf_bytes(title, design_id="CNtest0001", verts=6, tris=2):
    import zipfile, io
    xml = f'''<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
<metadata name="Title">{title}</metadata><metadata name="DesignModelId">{design_id}</metadata>
<resources><object id="1"><mesh><vertices>{''.join(f'<vertex x="{i}" y="0" z="0"/>' for i in range(verts))}</vertices>
<triangles>{''.join('<triangle v1="0" v2="1" v3="2"/>' for _ in range(tris))}</triangles></mesh></object></resources>
<build><item objectid="1"/></build></model>'''
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", xml)
    return buf.getvalue()


def test_upload_parse_and_hashdup(client):
    # 上传高达
    r = fetch(client, "/api/upload", files=[("file", "gundam.3mf", _make_3mf_bytes("高达RX78", "CNg1"))])
    assert r["results"][0]["ok"]
    f = r["results"][0]["file"]
    assert f["category"] == "IP·高达"
    assert f["design_id"] == "CNg1"
    assert len(f["sha256"]) == 64
    # 再次上传同一内容 -> 应提示重复
    r2 = fetch(client, "/api/upload", files=[("file", "gundam_copy.3mf", _make_3mf_bytes("高达RX78", "CNg1"))])
    assert r2["results"][0]["is_duplicate"] is True


def test_delete_blocks_earliest_dup(client):
    """同内容组最早副本是保留份：/api/delete 必须拒绝；删掉其他副本后放行。"""
    r1 = fetch(client, "/api/upload", files=[("file", "dup_a.3mf", _make_3mf_bytes("重复内容", "CNdup9"))])
    r2 = fetch(client, "/api/upload", files=[("file", "dup_b.3mf", _make_3mf_bytes("重复内容", "CNdup9"))])
    fa, fb = r1["results"][0]["file"], r2["results"][0]["file"]
    rows = {f["id"]: f for f in fetch(client, "/api/files")["files"]}
    earliest_id = fa["id"] if rows[fa["id"]]["is_earliest"] else fb["id"]
    other_id = fb["id"] if earliest_id == fa["id"] else fa["id"]
    # 最早副本拒绝删除
    blocked = fetch(client, "/api/delete", data={"id": earliest_id})
    assert "最早" in blocked.get("error", "")
    # 该 id 仍在库中
    assert any(f["id"] == earliest_id for f in fetch(client, "/api/files")["files"])
    # 非最早副本可删；组只剩一份后，原最早副本恢复可删
    assert fetch(client, "/api/delete", data={"id": other_id})["ok"] is True
    assert fetch(client, "/api/delete", data={"id": earliest_id})["ok"] is True


def test_upload_apply_and_query(client):
    r = fetch(client, "/api/upload", files=[("file", "horsy.3mf", _make_3mf_bytes("马年小马", "CNh1"))])
    fid = r["results"][0]["file"]["id"]
    # 归档
    res = fetch(client, "/api/apply", data={"ids": [fid], "rename": True})
    assert res["results"][0]["ok"]
    # 状态已应用
    f = fetch(client, "/api/files?status=applied")
    assert len(f["files"]) == 1
    assert f["files"][0]["status"] == "applied"
    # 本地路径存在（已移动）
    assert os.path.exists(f["files"][0]["abs_path"])


def test_tag_and_query(client):
    r = fetch(client, "/api/upload", files=[("file", "pika.3mf", _make_3mf_bytes("皮卡丘", "CNp1"))])
    fid = r["results"][0]["file"]["id"]
    fetch(client, "/api/tags", data={"id": fid, "tags": ["宝可梦", "可爱"]})
    got = fetch(client, "/api/files?tag=%E5%AE%9D%E5%8F%AF%E6%A2%A6")
    assert any(x["id"] == fid for x in got["files"])


def test_attachment_flow(client):
    r = fetch(client, "/api/upload", files=[("file", "m.3mf", _make_3mf_bytes("模型", "CNa1"))])
    fid = r["results"][0]["file"]["id"]
    res = fetch(client, "/api/attach", data={"id": str(fid)}, files=[("file", "notes.txt", b"print instructions")])
    assert res["ok"]
    atts = fetch(client, f"/api/attachments?file_id={fid}")
    assert len(atts["attachments"]) == 1
    assert atts["attachments"][0]["name"] == "notes.txt"


def test_stats_and_config(client):
    st = fetch(client, "/api/stats")
    assert "total" in st and "categories" in st
    cfg = fetch(client, "/api/config")
    assert "llm" in cfg and "paths" in cfg
