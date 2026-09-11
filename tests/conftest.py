# -*- coding: utf-8 -*-
"""pytest fixtures：为测试隔离临时工作目录与配置。"""
import os, sys, threading
import pytest

# 确保项目根在 sys.path（flat 布局）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)



@pytest.fixture()
def tmp_workspace(tmp_path):
    """提供隔离的临时工作目录。"""
    root = tmp_path / "libroot"
    root.mkdir()
    inbox = root / "00_待整理"
    inbox.mkdir()
    return {"root": str(root), "inbox": str(inbox)}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """启动隔离环境的测试服务器，返回可 fetch 的 base_url。"""
    import server
    root = tmp_path / "libroot"
    root.mkdir()
    # 隔离运行时路径
    monkeypatch.setattr(server, "LIBRARY_ROOT", str(root))
    monkeypatch.setattr(server, "INBOX", str(root / "00_待整理"))
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(server, "THUMB_DIR", str(tmp_path / "thumbs"))
    monkeypatch.setattr(server, "ATTACH_DIR", str(tmp_path / "attach"))
    server.init_db()
    # 启动服务
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base
    srv.shutdown()
    srv.server_close()


def _write_3mf(path, title="", designer="", design_id="", verts=0, tris=0, has_slice=False):
    """构造一个最小的、可被 parse_3mf 解析的 3MF（ZIP 含 3D/3dmodel.model）。"""
    import zipfile, io
    model_xml = f'''<?xml version="1.0" encoding="utf-8"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <metadata name="Title">{title}</metadata>
  <metadata name="Designer">{designer}</metadata>
  <metadata name="DesignModelId">{design_id}</metadata>
  <resources>
    <object id="1"><mesh>
      <vertices>{''.join(f'<vertex x="{i}" y="0" z="0"/>' for i in range(verts))}</vertices>
      <triangles>{''.join('<triangle v1="0" v2="1" v3="2"/>' for _ in range(tris))}</triangles>
    </mesh></object>
  </resources>
  <build><item objectid="1"/></build>
</model>'''
    names = ["3D/3dmodel.model"]
    if has_slice:
        names.append("Metadata/slice_info.config")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.writestr(n, model_xml if n.endswith(".model") else "dummy")
    with open(path, "wb") as f:
        f.write(buf.getvalue())


@pytest.fixture()
def make_3mf():
    return _write_3mf
