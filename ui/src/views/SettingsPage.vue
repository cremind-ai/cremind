<script setup lang="ts">
import { computed } from 'vue';
import { useRouter } from 'vue-router';
import { goBackToChat } from '../utils/backToChat';
import { ElCard } from 'element-plus';
import { Icon } from '@iconify/vue';

const props = defineProps<{ profile: string }>();
const router = useRouter();

interface SettingsCard {
  title: string;
  description: string;
  icon: string;
  route: string;
  /** When true, only the admin profile can see this card. */
  adminOnly?: boolean;
}

const allCards: SettingsCard[] = [
  {
    title: 'LLM Providers',
    description: 'Manage AI model providers, API keys, and model group assignments',
    icon: 'mdi:brain',
    route: 'llm',
  },
  {
    title: 'Tools & Skills',
    description: 'Built-in tools, skills, MCP servers, and A2A agent connections',
    icon: 'mdi:toolbox',
    route: 'tools-skills',
  },
  {
    title: 'Config',
    description: 'Tune agent reasoning, history limits, classifier and summarizer parameters',
    icon: 'mdi:tune',
    route: 'config',
  },
  {
    title: 'Vector Embedding',
    description: 'Enable semantic search, choose the embedding model, and configure the vector store',
    icon: 'mdi:vector-square',
    route: 'embedding',
    adminOnly: true,
  },
  {
    title: 'Channels',
    description: 'Connect Telegram, WhatsApp, Discord, Slack, Messenger, and Zalo',
    icon: 'mdi:link-variant',
    route: 'channels',
  },
  {
    title: 'Profiles',
    description: 'Manage user profiles and reconfigure the system',
    icon: 'mdi:account-group',
    route: 'profiles',
  },
];

// Vector Embedding is a server-wide configuration owned by the admin
// profile (the backend enforces this with require_admin). Hide it from
// non-admin profiles so they don't see a card that 403s on click.
const isAdmin = computed(() => props.profile === 'admin');
const settingsCards = computed(() =>
  allCards.filter(card => !card.adminOnly || isAdmin.value),
);

function navigateTo(route: string) {
  router.push(`/${props.profile}/settings/${route}`);
}

function goBack() {
  goBackToChat(router, props.profile);
}
</script>

<template>
  <div class="settings-page">
    <div class="settings-container">
      <div class="settings-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" />
          Back to Chat
        </button>
        <h1 class="settings-title">Settings</h1>
        <p class="settings-subtitle">Profile: <strong>{{ profile }}</strong></p>
      </div>

      <div class="settings-grid">
        <ElCard
          v-for="card in settingsCards"
          :key="card.route"
          class="settings-card"
          shadow="hover"
          @click="navigateTo(card.route)"
        >
          <div class="card-content">
            <div class="card-icon">
              <Icon :icon="card.icon" />
            </div>
            <div class="card-info">
              <h3 class="card-title">{{ card.title }}</h3>
              <p class="card-description">{{ card.description }}</p>
            </div>
            <Icon icon="mdi:chevron-right" class="card-chevron" />
          </div>
        </ElCard>
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings-page {
  width: 100%; height: 100%; overflow-y: auto; background: var(--bg-color);
  padding: 24px; box-sizing: border-box;
}
.settings-container { max-width: 720px; margin: 0 auto; }
.settings-header { margin-bottom: 32px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 16px; transition: color 0.2s;
}
.back-btn:hover { color: var(--primary-color); }
.settings-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0 0 4px 0; }
.settings-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; }
.settings-grid { display: flex; flex-direction: column; gap: 12px; }
.settings-card { cursor: pointer; transition: all 0.2s ease; background: var(--surface-color); }
.settings-card:hover { border-color: var(--primary-color); }
.card-content { display: flex; align-items: center; gap: 16px; }
.card-icon {
  width: 44px; height: 44px; display: flex; align-items: center; justify-content: center;
  background: var(--hover-bg); border-radius: 10px; font-size: 22px;
  color: var(--primary-color); flex-shrink: 0;
}
.card-info { flex: 1; }
.card-title { font-size: 1rem; font-weight: 600; color: var(--text-primary); margin: 0 0 2px 0; }
.card-description { font-size: 0.8rem; color: var(--text-secondary); margin: 0; line-height: 1.4; }
.card-chevron { font-size: 20px; color: var(--text-tertiary); flex-shrink: 0; }
</style>
