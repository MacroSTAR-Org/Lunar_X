/* Bot 端配置（含 AI 对话） */
window.Pages = window.Pages || {};
window.Pages.Bot = {
    name: 'BotPage',
    template: `
    <div>
      <h2 class="page-title">Bot 端配置</h2>
      <p class="page-sub">框架基础参数与 AI 对话设置</p>

      <el-card shadow="never" class="form-section">
        <div class="form-section-title">框架基础</div>
        <el-form label-position="top">
          <el-row :gutter="16">
            <el-col :span="8">
              <el-form-item label="机器人名称">
                <el-input v-model="form.bot_name" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="英文名称">
                <el-input v-model="form.bot_name_en" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="命令触发词">
                <el-input v-model="form.trigger_keyword" placeholder="$" />
                <div class="form-hint">如 $，消息以 $ 开头视为命令</div>
              </el-form-item>
            </el-col>
          </el-row>
          <el-row :gutter="16">
            <el-col :span="8">
              <el-form-item label="插件热重载">
                <el-select v-model="form.auto_reload_plugins">
                  <el-option label="开启" :value="true" />
                  <el-option label="关闭" :value="false" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="日志级别">
                <el-select v-model="form.log_level">
                  <el-option v-for="lv in ['DEBUG','INFO','WARNING','ERROR']" :key="lv" :label="lv" :value="lv" />
                </el-select>
              </el-form-item>
            </el-col>
          </el-row>
        </el-form>
      </el-card>

      <el-card shadow="never">
        <div class="form-section-title">AI 对话</div>
        <el-form label-position="top">
          <el-row :gutter="16">
            <el-col :span="6">
              <el-form-item label="启用 AI 对话">
                <el-select v-model="ai.enabled">
                  <el-option label="开启" :value="true" />
                  <el-option label="关闭" :value="false" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :span="10">
              <el-form-item label="API 地址（OpenAI 兼容）">
                <el-input v-model="ai.api_base" placeholder="https://api.deepseek.com/v1" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="API Key">
                <el-input v-model="ai.api_key" show-password placeholder="sk-..." />
              </el-form-item>
            </el-col>
          </el-row>
          <el-row :gutter="16">
            <el-col :span="8">
              <el-form-item label="模型">
                <el-input v-model="ai.model" placeholder="deepseek-chat" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="Temperature">
                <el-input-number v-model="ai.temperature" :min="0" :max="2" :step="0.1" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="Max Tokens">
                <el-input-number v-model="ai.max_tokens" :min="64" :max="8192" :step="64" />
              </el-form-item>
            </el-col>
          </el-row>
          <el-row :gutter="16">
            <el-col :span="8">
              <el-form-item label="历史消息条数">
                <el-input-number v-model="ai.max_history" :min="0" :max="50" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="私聊直连">
                <el-select v-model="ai.direct_chat">
                  <el-option label="开启" :value="true" />
                  <el-option label="关闭" :value="false" />
                </el-select>
              </el-form-item>
            </el-col>
          </el-row>
          <el-form-item label="系统提示词（System Prompt）">
            <el-input v-model="ai.system_prompt" type="textarea" :rows="3" />
          </el-form-item>
          <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
        </el-form>
      </el-card>
    </div>
    `,
    data() {
        return {
            raw: null,
            form: { bot_name: '', bot_name_en: '', trigger_keyword: '$', auto_reload_plugins: true, log_level: 'INFO' },
            ai: { enabled: false, api_base: '', api_key: '', model: '', system_prompt: '', temperature: 1.0, max_tokens: 2048, max_history: 10, direct_chat: false },
            saving: false,
        };
    },
    mounted() { this.load(); },
    methods: {
        async load() {
            try {
                const resp = await fetch('/api/config/config', { cache: 'no-store' });
                if (resp.status === 401) { location.href = '/login'; return; }
                this.raw = await resp.json();
                const f = this.raw;
                this.form = {
                    bot_name: f.bot_name || '', bot_name_en: f.bot_name_en || '',
                    trigger_keyword: f.trigger_keyword || '$',
                    auto_reload_plugins: f.auto_reload_plugins !== false,
                    log_level: f.log_level || 'INFO',
                };
                const a = f.ai || {};
                this.ai = {
                    enabled: a.enabled !== false,
                    api_base: a.api_base || '',
                    api_key: a.api_key || '',
                    model: a.model || '',
                    system_prompt: a.system_prompt || '',
                    temperature: a.temperature != null ? a.temperature : 1.0,
                    max_tokens: a.max_tokens || 2048,
                    max_history: a.max_history != null ? a.max_history : 10,
                    direct_chat: a.direct_chat === true,
                };
            } catch (e) {
                this.$message.error('加载配置失败: ' + e.message);
            }
        },
        async save() {
            if (!this.raw) return;
            this.saving = true;
            try {
                const out = JSON.parse(JSON.stringify(this.raw));
                out.bot_name = this.form.bot_name;
                out.bot_name_en = this.form.bot_name_en;
                out.trigger_keyword = this.form.trigger_keyword;
                out.auto_reload_plugins = this.form.auto_reload_plugins;
                out.log_level = this.form.log_level;
                out.ai = Object.assign({}, out.ai || {}, this.ai);
                const resp = await fetch('/api/config/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(out),
                });
                const data = await resp.json();
                if (resp.ok) this.$message.success(data.message || '保存成功');
                else this.$message.error(data.error || '保存失败');
            } catch (e) {
                this.$message.error('保存失败: ' + e.message);
            } finally {
                this.saving = false;
            }
        },
    },
};
