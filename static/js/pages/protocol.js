/* 协议端配置 */
window.Pages = window.Pages || {};
window.Pages.Protocol = {
    name: 'ProtocolPage',
    template: `
    <div>
      <h2 class="page-title">协议端配置</h2>
      <p class="page-sub">Milky 协议端（LLBot / Lagrange）连接参数</p>

      <el-card shadow="never">
        <div class="form-section-title">协议端（Milky Server）</div>
        <el-form label-position="top">
          <el-form-item label="签名服务器 URL（SignServerUrl）">
            <el-input v-model="form.SignServerUrl" placeholder="https://sign.lagrangecore.org/api/sign/39038" />
            <div class="form-hint">签名服务地址，与协议端版本匹配</div>
          </el-form-item>
          <el-row :gutter="16">
            <el-col :span="8">
              <el-form-item label="监听 Host">
                <el-input v-model="impl.Host" placeholder="127.0.0.1" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="端口">
                <el-input v-model.number="impl.Port" placeholder="3010" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="AccessToken">
                <el-input v-model="impl.AccessToken" placeholder="协议端鉴权 token" show-password />
              </el-form-item>
            </el-col>
          </el-row>
          <el-form-item label="机器人 QQ（Uin）">
            <el-input v-model.number="account.Uin" placeholder="QQ 号" />
          </el-form-item>
          <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
        </el-form>
      </el-card>
    </div>
    `,
    data() {
        return {
            form: { SignServerUrl: '' },
            impl: { Host: '127.0.0.1', Port: 3010, AccessToken: '' },
            account: { Uin: 0 },
            raw: null,
            saving: false,
        };
    },
    mounted() { this.load(); },
    methods: {
        async load() {
            try {
                const resp = await fetch('/api/config/appsettings', { cache: 'no-store' });
                if (resp.status === 401) { location.href = '/login'; return; }
                this.raw = await resp.json();
                this.form.SignServerUrl = this.raw.SignServerUrl || '';
                const impls = this.raw.Implementations || [];
                this.impl = (impls[0] && { Host: impls[0].Host, Port: impls[0].Port, AccessToken: impls[0].AccessToken })
                    || { Host: '127.0.0.1', Port: 3010, AccessToken: '' };
                this.account = this.raw.Account || { Uin: 0 };
            } catch (e) {
                this.$message.error('加载配置失败: ' + e.message);
            }
        },
        async save() {
            if (!this.raw) return;
            this.saving = true;
            try {
                const out = JSON.parse(JSON.stringify(this.raw));
                out.SignServerUrl = this.form.SignServerUrl;
                if (!out.Implementations || !out.Implementations.length) out.Implementations = [{}];
                out.Implementations[0].Host = this.impl.Host;
                out.Implementations[0].Port = Number(this.impl.Port) || 0;
                out.Implementations[0].AccessToken = this.impl.AccessToken;
                out.Account = Object.assign({}, this.account);
                const resp = await fetch('/api/config/appsettings', {
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
