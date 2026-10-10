# docs/images

README 里引用的截图。**所有图片在入库前都做过隐私清理**，说明如下。

| 文件 | 内容 | 来源 |
| --- | --- | --- |
| `api-docs.png` | 四层 API 分组（系统 / 认证 / 经营分析 / 可观测 / 工作流） | Playwright 直接截取 `/docs` 页面 |
| `demo-quality-control.png` | 演示第 3 部分：质控层检出缺货 / 改价 / 投放递减 | `python scripts/demo.py` |
| `demo-overview.png` | 演示开篇：数据基座与 4 类陷阱位置 | `python scripts/demo.py` |
| `verify-coverage.png` | 架构验收：Registry / 容错 / 意图 / 时间错位 | `python scripts/verify_architecture.py` |
| `verify-result.png` | 架构验收结论：184 项通过，0 项失败 | `python scripts/verify_architecture.py` |

## 隐私清理说明

公开仓库的每一张图都会被看到，所以入库前逐张确认过不含以下信息：

1. **命令行提示符** —— 终端截图底部原本有 `PS D:\...\ec-monica>`，
   会暴露本机目录结构。已裁掉底部若干像素行。
2. **浏览器 chrome** —— 若用带界面的浏览器截图，会带上地址栏、标签页和**书签栏**
   （书签栏会暴露个人浏览习惯）。所以 API 文档图改用 Playwright 直接截取页面，
   天然不含浏览器界面。
3. **IDE 面包屑保留** —— 截图保留了 `ec-monica / main` 这一行，
   因为它展示的是项目上下文（仓库名与分支），有用且不敏感。

## 重新生成

终端类截图：

```bash
python scripts/seed_data.py
python scripts/demo.py            # 终端有 ANSI 配色，截图效果好
python scripts/verify_architecture.py
```

API 文档截图（需服务已启动）：

```bash
uvicorn app.main:app &
playwright-cli open http://127.0.0.1:8000/docs --browser=msedge
playwright-cli resize 1500 1750
playwright-cli screenshot --filename=docs/images/api-docs.png
playwright-cli close
```

> ⚠️ 重新生成终端截图后，**记得再裁一次底部提示符行**再提交。
