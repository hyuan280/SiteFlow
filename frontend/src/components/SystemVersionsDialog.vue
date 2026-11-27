<script setup lang="ts">
import { computed, ref } from "vue";

import { ApiError, apiRequest } from "@/api/client";

interface ComponentBuild {
  version: string | null;
  commit: string | null;
}

interface SystemVersions {
  api: ComponentBuild;
  worker: ComponentBuild | null;
  bitwarden: ComponentBuild | null;
}

declare const __SITEFLOW_WEB_BUILD__: ComponentBuild;

const visible = defineModel<boolean>({ required: true });
const emit = defineEmits<{ unauthorized: [] }>();
const versions = ref<SystemVersions>();
const loading = ref(false);
const loadError = ref("");
const rows = computed(() => [
  { name: "Web 前端", build: __SITEFLOW_WEB_BUILD__ },
  { name: "API 后端", build: versions.value?.api },
  { name: "Worker", build: versions.value?.worker },
  { name: "Bitwarden", build: versions.value?.bitwarden },
]);

async function loadVersions(): Promise<void> {
  if (loading.value) return;
  loading.value = true;
  loadError.value = "";
  try {
    versions.value = await apiRequest<SystemVersions>("/system/version");
  } catch (error) {
    versions.value = undefined;
    if (error instanceof ApiError && error.status === 401) {
      emit("unauthorized");
    } else {
      loadError.value = "无法读取服务版本";
    }
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="关于 SiteFlow"
    width="min(620px, 92vw)"
    append-to-body
    @open="loadVersions"
  >
    <el-alert v-if="loadError" :title="loadError" type="warning" :closable="false" />
    <el-table :data="rows" aria-label="组件版本">
      <el-table-column prop="name" label="组件" min-width="80" />
      <el-table-column label="版本" min-width="80">
        <template #default="{ row }">
          {{ row.build ? (row.build.version ?? "未知") : (loading ? "读取中…" : "暂不可用") }}
        </template>
      </el-table-column>
      <el-table-column label="Commit" min-width="100">
        <template #default="{ row }">
          <code v-if="row.build?.commit" class="commit">{{ row.build.commit }}</code>
          <span v-else>—</span>
        </template>
      </el-table-column>
    </el-table>
    <template #footer>
      <el-button :loading="loading" @click="loadVersions">刷新版本</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.commit {
  overflow-wrap: anywhere;
  user-select: text;
}
</style>
