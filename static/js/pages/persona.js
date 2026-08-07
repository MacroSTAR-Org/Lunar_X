/* 人格设置 */
window.Pages = window.Pages || {};
window.Pages.Persona = {
    name: 'PersonaPage',
    template: `
    <div>
      <h2 class="page-title">人格设置</h2>
      <p class="page-sub">AI 助手的人设、唤醒词与回复风格</p>

      <el-card shadow="never">
        <el-form label-position="top">
          <el-form-item label="使用名字唤醒">
            <el-select v-model="enabled" style="width: 200px;">
              <el-option label="开启（被叫到名字时才主动回复）" :value="true" />
              <el-option label="关闭（不启用名字唤醒）" :value="false" />
            </el-select>
            <div class="form-hint">开启后，消息以唤醒词开头才会触发 AI 回复</div>
          </el-form-item>
          <el-form-item label="唤醒词（每行一个）">
            <el-input v-model="nicknamesText" type="textarea" :rows="4" placeholder="小月&#10;月月" />
            <div class="form-hint">群聊中呼叫这些名字会触发 AI 主动回复</div>
          </el-form-item>
          <el-form-item label="人格设定（System Prompt）">
            <el-input v-model="prompt" type="textarea" :rows="6" placeholder="你是小月，一个温柔可爱的 AI 助手……" />
            <div class="form-hint">此提示词优先于 Bot 配置页的全局 System Prompt</div>
          </el-form-item>
          <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
        </el-form>
      </el-card>
    </div>
    `,
    data() {
        return {
            raw: null,
            enabled: false,
            nicknamesText: '',
            prompt: '',
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
                const a = this.raw.ai || {};
                const p = a.persona || {};
                this.enabled = p.enabled !== false && !!(p.nicknames && p.nicknames.length);
                this.nicknamesText = (p.nicknames || []).join('\n');
                this.prompt = p.system_prompt || '';
            } catch (e) {
                this.$message.error('加载配置失败: ' + e.message);
            }
        },
        async save() {
            if (!this.raw) return;
            this.saving = true;
            try {
                const out = JSON.parse(JSON.stringify(this.raw));
                const ai = out.ai || {};
                ai.persona = Object.assign({}, ai.persona || {}, {
                    enabled: this.enabled,
                    nicknames: this.nicknamesText.split('\n').map(s => s.trim()).filter(Boolean),
                    system_prompt: this.prompt,
                });
                out.ai = ai;
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
