# ✨ 待办管理 v2

多用户待办管理系统，Python + 单页 HTML，零依赖部署。

## 功能

- 🔐 多用户注册/登录，数据完全隔离
- 📝 待办 CRUD、进展记录、附件管理
- 🤖 AI 智能拆解（智谱 GLM）、AI 工作总结
- 🏷️ 自定义项目分类（增删）
- 📊 Excel 导出
- 🔔 提醒 + 京ME 推送
- 📌 便签侧边栏（localStorage）
- 📤 批量导入

## 本地运行

```bash
python3 server.py
# 打开 http://127.0.0.1:19101
```

## 部署到 Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/sunyayiii/ccsun-todolist)

1. 点击上方按钮或前往 [render.com](https://render.com)
2. New → Web Service → 连接此 GitHub 仓库
3. 自动识别配置，点 Deploy 即可
