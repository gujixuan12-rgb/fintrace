"""sourcing 层：从公开披露源取原始材料（当前实现巨潮资讯网）。

设计约束（与项目其余部分一致）：

* 零第三方依赖 —— 只用标准库 `urllib`。这台机器上 curl 写文件会失败
  （`curl: (23) client returned ERROR on write`），所以不要用 curl 取数。
* 取到的每份材料都要能回答「从哪来」—— manifest 记来源 URL、公告 ID、
  披露时间、字节数与 SHA256。这是「可追溯」在取数环节的对应物。
* 取数不参与判定。这里只负责把原始文件搬回本地、登记造册；
  任何数值判定仍走 `calculators/` / `verification/`。
"""
