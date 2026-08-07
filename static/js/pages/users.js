/* 用户管理 */
window.Pages = window.Pages || {};
window.Pages.Users = {
    name: 'UsersPage',
    template: `
    <div>
      <h2 class="page-title">用户管理</h2>
      <p class="page-sub">管理员与超级管理员 QQ 号（逗号分隔）</p>

      <el-card shadow="never">
        <el-form label-position="top">
          <el-form-item label="超级管理员（super_users）">
            <el-input v-model="form.super_users" type="textarea" :rows="3" placeholder="123456, 789012" />
            <div class="form-hint">拥有全部权限，包括重启机器人、删除语录等</div>
          </el-form-item>
          <el-form-item label="管理员（manager_users）">
            <el-input v-model="form.manager_users" type="textarea" :rows="3" placeholder="123456, 789012" />
            <div class="form-hint">可执行禁言、踢人、删除语录等群管理操作</div>
          </el-form-item>
          <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
        </el-form>
      </el-card>
    </div>
    `,
    data() {
        return {
            raw: null,
            form: { super_users: '', manager_users: '' },
            saving: false,
        };
    },
    mounted() { this.load(); },
    methods: {
        async load() {
            try {
                const resp = await fetch('/api/config/admin', { cache: 'no-store' });
                if (resp.status === 401) { location.href = '/login'; return; }
                this.raw = await resp.json();
                this.form.super_users = (this.raw.super_users || []).join(', ');
                this.form.manager_users = (this.raw.manager_users || []).join(', ');
            } catch (e) {
                this.$message.error('加载配置失败: ' + e.message);
            }
        },
        parseList(s) {
            return s.split(/[,，\s]+/).map(x => x.trim()).filter(Boolean).map(Number).filter(n => !isNaN(n));
        },
        async save() {
            if (!this.raw) return;
            this.saving = true;
            try {
                const out = JSON.parse(JSON.stringify(this.raw));
                out.super_users = this.parseList(this.form.super_users);
                out.manager_users = this.parseList(this.form.manager_users);
                const resp = await fetch('/api/config/admin', {
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
