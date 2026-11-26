<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { ApiError, apiRequest } from "@/api/client";
import type { SiteSummary } from "@/types/workflow";

interface Server {
  id: string;
  name: string;
  site_keys: string[];
  all_sites: boolean;
  created_at: string;
}

interface Connection {
  uuid: string;
  password: string;
}

const emit = defineEmits<{ unauthorized: [error: ApiError] }>();
const endpoint = "/cookiecloud/servers";
const servers = ref<Server[]>([]);
const sites = ref<SiteSummary[]>([]);
const endpointBase = ref("");
const ready = ref(false);
const busy = ref(false);
const error = ref("");
const formError = ref("");
const dialogOpen = ref(false);
const editingId = ref<string | null>(null);
const savedConnection = ref<Connection | null>(null);
const emptyForm = () => ({ id: "", name: "", site_keys: [] as string[], all_sites: false, uuid: "", password: "" });
const form = ref(emptyForm());
const siteOptions = computed(() => sites.value.map((site) => ({ label: site.name, value: site.history_key })));
const urlFor = (id: string) => `${endpointBase.value || window.location.origin}/cookiecloud/${id}`;

function selectAllSites() {
  form.value.site_keys = siteOptions.value.map((site) => site.value);
}

function clearSites() {
  form.value.site_keys = [];
}

function hex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function generateUuid() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const value = hex(bytes);
  form.value.uuid = `${value.slice(0, 8)}-${value.slice(8, 12)}-${value.slice(12, 16)}-${value.slice(16, 20)}-${value.slice(20)}`;
}

function generatePassword() {
  form.value.password = hex(crypto.getRandomValues(new Uint8Array(24)));
}

function report(failure: unknown, target = error) {
  if (failure instanceof ApiError && failure.status === 401) emit("unauthorized", failure);
  target.value = failure instanceof ApiError ? failure.message : "无法连接 CookieCloud 设置服务。";
}

async function load() {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const [serverRows, siteRows, endpointConfig] = await Promise.all([
      apiRequest<Server[]>(endpoint), apiRequest<SiteSummary[]>("/sites"),
      apiRequest<{ base_url: string }>(`${endpoint}/endpoint-base`),
    ]);
    servers.value = serverRows;
    sites.value = siteRows;
    endpointBase.value = endpointConfig.base_url;
    ready.value = true;
  } catch (failure) {
    report(failure);
  } finally {
    busy.value = false;
  }
}

async function openEditor(server?: Server) {
  error.value = "";
  formError.value = "";
  editingId.value = server?.id ?? null;
  form.value = emptyForm();
  if (server) {
    busy.value = true;
    try {
      savedConnection.value = await apiRequest<Connection>(`${endpoint}/${server.id}/connection`);
      form.value = { ...server, site_keys: [...server.site_keys], ...savedConnection.value };
    } catch (failure) {
      report(failure);
      return;
    } finally {
      busy.value = false;
    }
  } else {
    generateUuid();
    generatePassword();
  }
  dialogOpen.value = true;
}

function clearEditor() {
  if (dialogOpen.value) return;
  form.value = emptyForm();
  savedConnection.value = null;
}

async function save() {
  if (busy.value) return;
  const { id, name, site_keys, all_sites, uuid, password } = form.value;
  if (!/^[a-z0-9_-]{1,64}$/.test(id) || !name.trim() || !/^[a-zA-Z0-9_-]{1,128}$/.test(uuid) || !password) {
    formError.value = "请填写有效的 ID、显示名称、UUID 和加密密码。";
    return;
  }
  const currentId = editingId.value;
  const body = {
    name: name.trim(), all_sites, site_keys: all_sites ? [] : site_keys, uuid,
    ...(password === savedConnection.value?.password ? {} : { password }),
  };
  busy.value = true;
  formError.value = "";
  try {
    const saved = await apiRequest<Server>(currentId ? `${endpoint}/${currentId}` : endpoint, {
      method: currentId ? "PUT" : "POST",
      body: JSON.stringify(currentId ? body : { ...body, id }),
    });
    if (currentId) {
      servers.value = servers.value.map((server) => server.id === currentId ? saved : server);
    } else {
      servers.value = [...servers.value, saved];
    }
    dialogOpen.value = false;
    ElMessage.success("CookieCloud 配置已保存");
  } catch (failure) {
    report(failure, formError);
  } finally {
    busy.value = false;
  }
}

async function remove(server: Server) {
  try {
    await ElMessageBox.confirm(`删除「${server.name}」后，此配置的客户端将无法继续拉取 Cookie。`, "删除 CookieCloud 配置", {
      type: "warning", confirmButtonText: "删除", cancelButtonText: "取消",
    });
  } catch {
    return;
  }
  busy.value = true;
  error.value = "";
  try {
    await apiRequest<void>(`${endpoint}/${server.id}`, { method: "DELETE" });
    servers.value = servers.value.filter(({ id }) => id !== server.id);
  } catch (failure) {
    report(failure);
  } finally {
    busy.value = false;
  }
}

onMounted(load);
</script>

<template>
  <el-card shadow="never" class="panel" v-loading="busy && !dialogOpen">
    <template #header>
      <div class="card-head">
        <strong>CookieCloud</strong>
        <div>
          <el-button :disabled="busy" @click="load">刷新</el-button>
          <el-button type="primary" :disabled="busy || !ready" @click="openEditor()">添加服务器</el-button>
        </div>
      </div>
    </template>
    <p class="muted">供 CookieCloud 客户端下载所选站点已保存的 Cookie。每个 ID 对应一个独立的服务地址。</p>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <el-table v-if="ready" :data="servers" row-key="id" empty-text="暂无 CookieCloud 服务器">
      <el-table-column prop="name" label="显示名称" min-width="140" />
      <el-table-column label="API 端点" min-width="270">
        <template #default="{ row }"><code class="endpoint-url">{{ urlFor(row.id) }}</code></template>
      </el-table-column>
      <el-table-column label="已选站点" width="100">
        <template #default="{ row }">{{ row.all_sites ? "所有站点" : row.site_keys.length }}</template>
      </el-table-column>
      <el-table-column label="操作" width="140">
        <template #default="{ row }">
          <el-button link type="primary" :disabled="busy" @click="openEditor(row)">配置</el-button>
          <el-button link type="danger" :disabled="busy" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>

  <el-dialog
    v-model="dialogOpen" :title="editingId ? '配置 CookieCloud 服务器' : '添加 CookieCloud 服务器'"
    width="min(600px, 94vw)" destroy-on-close :close-on-click-modal="false"
    :show-close="!busy" :close-on-press-escape="!busy" @closed="clearEditor"
  >
    <el-form label-position="top" :disabled="busy" @submit.prevent="save">
      <el-form-item label="显示名称" required>
        <el-input v-model="form.name" maxlength="128" placeholder="例如：家庭浏览器" />
      </el-form-item>
      <el-form-item label="服务器 ID" required>
        <el-input v-model="form.id" maxlength="64" :disabled="!!editingId" placeholder="小写字母、数字、连字符和下划线" />
      </el-form-item>
      <el-form-item v-if="form.id" label="客户端服务地址">
        <el-input :model-value="urlFor(form.id)" readonly />
      </el-form-item>
      <el-form-item label="提供 Cookie 的站点">
        <div class="site-selection">
          <el-checkbox v-model="form.all_sites">所有站点（自动包含以后新增的站点）</el-checkbox>
          <el-select-v2
            v-if="!form.all_sites"
            v-model="form.site_keys" :options="siteOptions" multiple filterable collapse-tags
            aria-label="提供 Cookie 的站点" placeholder="选择站点，留空则不提供 Cookie"
          />
          <div v-if="!form.all_sites" class="site-actions">
            <el-button size="small" :disabled="!siteOptions.length" @click="selectAllSites">全选</el-button>
            <el-button size="small" :disabled="!form.site_keys.length" @click="clearSites">清空</el-button>
            <span class="muted" aria-live="polite">已选 {{ form.site_keys.length }} / {{ siteOptions.length }} 个站点</span>
          </div>
        </div>
      </el-form-item>
      <el-form-item label="UUID" required>
        <el-input v-model="form.uuid" maxlength="128" autocomplete="off">
          <template #append><el-button @click="generateUuid">生成</el-button></template>
        </el-input>
      </el-form-item>
      <el-form-item label="端对端加密密码" required>
        <el-input v-model="form.password" type="password" show-password maxlength="256" autocomplete="new-password">
          <template #append><el-button @click="generatePassword">生成</el-button></template>
        </el-input>
      </el-form-item>
    </el-form>
    <p v-if="editingId" class="muted">更改 UUID 或密码后，请同步更新客户端配置。</p>
    <el-alert v-if="formError" :title="formError" type="error" :closable="false" show-icon />
    <template #footer>
      <el-button :disabled="busy" @click="dialogOpen = false">取消</el-button>
      <el-button type="primary" :loading="busy" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.card-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.endpoint-url { overflow-wrap: anywhere; }
.site-selection { display: grid; gap: 8px; width: 100%; }
.site-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.site-actions .el-button + .el-button { margin-left: 0; }
</style>
