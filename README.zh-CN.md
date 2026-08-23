<p align="right">
  <a href="./README.md">English</a> |
  <strong>简体中文</strong>
</p>

# Human Eval Platform

一个轻量级、可自行部署的平台，用于对视频或其他标注任务开展结构化人工评测。评测任务通过 JSON 定义，参与者进度和评测结果存储在 SQLite 中。

![平台界面概览](assets/platform-overview.svg)

本公开仓库仅包含通用演示、本地生成的媒体文件和合成配置，不包含研究数据、参与者记录、凭据、服务器地址或私有部署设置。

## 功能特性

- 可配置视频、说明、评测维度、问题、评分范围和置信度评分。
- 支持参与者白名单、持久化会话、随机视频顺序和草稿自动保存。
- 支持可滚动的视频字幕，以及文本引用、高亮和视频时间戳引用。
- 支持按顺序解锁任务，以及管理员发起修改请求。
- 支持以只读方式查看结果、按维度统计、计算总分和导出 CSV。
- 支持浏览器缓存、ETag、HTTP Range 请求、带宽控制和流量警报。
- 前端使用原生 HTML、CSS 和 JavaScript，后端使用 Python 标准库。

## 快速开始

需要 Python 3.10 或更高版本。

```bash
git clone <repository-url>
cd Human_Eval_Platform
export HEP_ADMIN_PASSWORD='replace-with-a-strong-password'
python3 app.py --config config.example.json
```

打开 `http://127.0.0.1:8000`。演示环境接受 `account-demo` 或 `account-test` 作为参与者标识符。

未设置 `HEP_ADMIN_PASSWORD` 时仍可使用评测流程，但管理和结果查看页面将被禁用。

## 创建评测任务

1. 将 `templates/evaluation_flow_template.json` 复制到 `data/flows/`。
2. 将视频和对应的字幕文件放到 `storage/videos/<task-name>/` 下。
3. 在工作流 JSON 中配置视频、评测维度、问题和评分规则。
4. 将获准参与评测的参与者标识符添加到 `docs/participant-allowlist.md`。
5. 重启服务器以导入新的工作流。

有关完整的数据结构，请参阅[工作流配置](templates/README.md)和[数据配置](docs/configuration-and-data.md)。

## 数据与部署

运行时数据存储在 `storage/human_eval.db` 中，并被排除在 Git 版本控制之外。本地配置、导出文件、生成的预览和参与者数据默认也会被忽略。

公开部署时，请使用 HTTPS、设置高强度管理员密码、启用安全 Cookie，并将服务置于生产级反向代理和防火墙之后。

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m py_compile app.py human_eval_platform/*.py
npm install
npm test
```

## 科研用途

本平台为某研究项目的人工评测阶段开发。正式论文发表后将补充引用信息。

## 许可证

平台代码基于 MIT 许可证发布。仓库中包含的演示媒体均为合成内容，不含任何第三方视频素材。
