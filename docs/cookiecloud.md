# CookieCloud 接入指南

SiteFlow 可以作为只读 CookieCloud 服务，把所选站点最近一次保存的 Cookie 提供给客户端。服务只返回 CookieCloud 密文，不接收上传，也不同步 Local Storage。

## 创建服务器

1. 打开「系统设置 → CookieCloud」，点击「添加服务器」。可以创建多条配置，列表按创建时间显示。
2. 填写显示名称和服务器 ID。ID 长度为 1–64 个字符，只允许小写字母、数字、连字符和下划线，创建后不能修改。
3. 选择共享范围：
   - **手动选择**：可以逐个选站点，也可以「全选」当前列表后去掉个别站点。「清空」后不会共享任何站点。以后新增的站点不会自动加入。
   - **所有站点**：勾选并保存后，现有站点和以后新增的站点都会纳入范围。只有已保存且未过期的 Cookie 才会下发。
4. 使用自动生成的 UUID 和加密密码，或手动填写后保存。更改 UUID 或密码后，需同步更新客户端配置。

完整端点形如 `https://siteflow.example.com/cookiecloud/home_1`，优先使用后端环境变量 `SITEFLOW_DOMAIN`，未配置时使用当前页面的访问地址。自定义反向代理时，使用 HTTPS，并将 `/cookiecloud/*` 原样转发到 SiteFlow API，避免记录包含 UUID 的拉取 URL。SiteFlow 在服务端生成 CookieCloud 密文，并使用主密钥加密保存连接凭据。

## 客户端接入

浏览器版 CookieCloud 选择「下载」，填入完整端点、UUID 和加密密码。Python 可以安装 `PyCookieCloud` 后使用以下示例（连接信息由环境变量传入）：

```python
import os

from PyCookieCloud import PyCookieCloud

cloud = PyCookieCloud(
    os.environ["COOKIECLOUD_URL"],       # https://siteflow.example.com/cookiecloud/home_1
    os.environ["COOKIECLOUD_UUID"],
    os.environ["COOKIECLOUD_PASSWORD"],
)

data = cloud.get_decrypted_data()
if data is None:
    print("连接、下载或解密失败")
else:
    for domain, cookies in data.items():
        print(domain, len(cookies))  # 只显示域名和数量
```

URL 填设置页显示的完整端点，无需拼接 `/get/<uuid>`。PyCookieCloud 1.0.4 会先 `GET /cookiecloud/<id>` 检查是否返回 200，再请求 `/cookiecloud/<id>/get/<uuid>` 并解密默认的 `legacy` 格式。

## 常见问题

拉取失败、返回空数据或取不到指定域名的 Cookie 时，见 [CookieCloud 故障排查](troubleshooting.md#cookiecloud-拉取或域名匹配失败)。
