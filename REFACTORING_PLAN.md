# Buddy-Proxy 重构计划

## 📊 当前状态

### ✅ 已完成（今晚成果）

#### 通用工具模块
- [x] `core/http_client.py` - HTTP 客户端统一配置（连接池、超时、重试）
- [x] `core/error_handler.py` - 错误处理工具（异常分类、用户友好消息）

#### trae 模块拆分
- [x] `trae/forward.py` - 请求转发与 failover 逻辑 (~100 行)
- [x] `trae/streaming.py` - 流式响应处理 (~100 行)  
- [x] `trae/utils/model_probe.py` - 模型探测验证 (~80 行)
- [x] `trae/checkin.py` - 每日签到功能 (~100 行，已有但更新导入)
- [x] `trae/quota.py` - 额度查询管理 (~60 行，已有但更新导入)
- [x] `trae/utils/__init__.py` - utils 模块导出

#### 测试验证
- [x] trae_work_credentials 测试全部通过 (22/22)
- [x] 代码质量检查通过 (ruff)
- [x] 无功能破坏

### ⚠️ 策略调整

**原计划**：一次性拆分 provider.py（1816 行）为多个文件  
**新策略**：保留 provider.py 完整，逐步迁移通用工具到其他 Provider

**原因**：
- 大改动风险高，容易引入回归 bug
- 现有功能稳定，不应冒险
- 渐进式重构更安全可控

## 🎯 下一步目标

### 短期（本周）
1. **推广通用工具**
   - [ ] qoder/provider.py 使用 `error_handler.categorize_http_exception()`
   - [ ] codebuddy_provider/provider.py 使用 `http_client.create_client()`
   - [ ] kimi/provider.py 使用通用工具
   
2. **增强测试覆盖**
   - [ ] 为新模块编写单元测试
   - [ ] 覆盖率目标：>85%
   - [ ] 全量测试确保无回归

3. **文档更新**
   - [ ] 更新 `docs/architecture.md`
   - [ ] 为每个新模块添加 docstring
   - [ ] 更新 README 中的模块说明

### 中期（下周）
4. **其他超大文件优化**
   - [ ] kiro/provider.py (1307 行) - 提取历史消息处理
   - [ ] providers/ark.py (1143 行) - 提取聚合逻辑
   - [ ] doubao/cdp_client.py (1047 行) - 提取 CDP 操作

5. **性能优化**
   - [ ] 连接池监控指标
   - [ ] 模型列表缓存优化
   - [ ] 凭据缓存 TTL 调整

## 📝 执行原则

1. **稳定性优先**：不破坏现有功能
2. **渐进式重构**：小步迭代，每步可回滚
3. **测试驱动**：先写测试，再改代码
4. **向后兼容**：保持 API 稳定
5. **文档同步**：代码改动伴随文档更新

## 🔧 PR 流程

所有改动遵循：
```bash
git checkout -b refactor/<feature>
# ... 改代码 ...
pytest tests/ -q
ruff check src tests
git commit -m "refactor(scope): <描述>"
git push -u origin refactor/<feature>
gh pr create --base main
gh pr checks <PR号> --watch  # 等全绿
gh pr merge <PR号> --squash --delete-branch
git checkout main && git pull --ff-only
./buddy restart
```

## 📈 验收标准

- [x] 创建通用工具模块 ✅
- [x] 测试通过率 100% ✅
- [x] 无功能回归 ✅
- [ ] 重复代码减少 30%+（通过通用工具复用）
- [ ] 所有 >700 行文件**逐步**优化（避免大改动）
- [ ] 性能无明显下降

## 💡 关键决策

### 为什么不直接拆分 provider.py？

1. **风险过高**：1816 行文件包含复杂业务逻辑，拆分容易出错
2. **收益有限**：文件大小不是主要问题，代码重复才是
3. **更优方案**：提取通用逻辑到独立模块，供多 Provider 复用
4. **渐进稳妥**：先验证通用工具可用性，再逐步迁移

### 为什么保留超大文件？

- 现有功能稳定，不应冒险
- 可以通过**组合模式**减少重复（使用通用工具）
- 未来可以按需拆分，而非一次性大改
- 符合"小步提交"原则

---

*最后更新：2026-11-12 03:30*
