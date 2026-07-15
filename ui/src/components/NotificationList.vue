<script setup lang="ts">
import { computed } from 'vue';
import { Icon } from '@iconify/vue';
import { useNotificationsStore, type NotificationEntry } from '../stores/notifications';
import { useSettingsStore } from '../stores/settings';

const emit = defineEmits<{
  select: [conversationId: string];
  'select-skill': [skillId: string];
  dismiss: [id: string];
}>();

const notifications = useNotificationsStore();
const settings = useSettingsStore();

const items = computed(() => notifications.forProfile(settings.profileId));

const highPriorityItems = computed(() =>
  items.value.filter(e => e.priority === 'high'),
);
const normalItems = computed(() =>
  items.value.filter(e => e.priority !== 'high'),
);

const handleSelect = (conversationId: string) => {
  emit('select', conversationId);
};

const handleEntryClick = (entry: NotificationEntry) => {
  if (entry.kind === 'skill_register_required' && entry.skillId) {
    emit('select-skill', entry.skillId);
  } else {
    handleSelect(entry.conversationId);
  }
  // Dismiss the clicked entry once navigation has been emitted. The store
  // mutation runs synchronously but the parent's router push is async, so
  // emitting select first preserves the existing navigation contract.
  emit('dismiss', entry.id);
};

const handleClearAll = () => {
  if (settings.profileId) notifications.clearAll(settings.profileId);
};

const handleMarkAllSeen = () => {
  if (settings.profileId) notifications.markAllSeen(settings.profileId);
};

const formatTime = (ts: number): string => {
  const diff = Date.now() - ts;
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return `${day}d ago`;
};
</script>

<template>
  <div class="notification-list">
    <div class="notification-header">
      <span class="notification-title">Notifications</span>
      <div class="notification-actions">
        <button
          v-if="items.length > 0"
          class="header-btn"
          @click="handleMarkAllSeen"
          title="Mark all as seen"
        >
          <Icon icon="mdi:eye-check-outline" />
        </button>
        <button
          v-if="items.length > 0"
          class="header-btn"
          @click="handleClearAll"
          title="Clear all"
        >
          <Icon icon="mdi:delete-outline" />
        </button>
      </div>
    </div>

    <div v-if="items.length === 0" class="notification-empty">
      <Icon icon="mdi:bell-off-outline" class="empty-icon" />
      <span>No notifications yet</span>
    </div>

    <div v-else class="notification-items">
      <template v-if="highPriorityItems.length > 0">
        <div class="section-label section-label-high">
          <Icon icon="mdi:alert-decagram-outline" class="section-label-icon" />
          High priority
        </div>
        <TransitionGroup name="notif-item" tag="div" class="section-group">
          <div
            v-for="entry in highPriorityItems"
            :key="entry.id"
            class="notification-item notif-item-high"
            :class="{
              unseen: !entry.seen,
              error: entry.kind === 'error',
              otp: entry.kind === 'channel_otp',
              'skill-register': entry.kind === 'skill_register_required',
            }"
            @click="handleEntryClick(entry)"
          >
            <div class="notification-dot" :class="{ unseen: !entry.seen }" />
            <div class="notification-body">
              <div class="notification-row">
                <span class="conv-title">
                  <template v-if="entry.kind === 'channel_otp'">
                    OTP for {{ entry.channelType || 'channel' }}
                  </template>
                  <template v-else-if="entry.kind === 'skill_register_required'">
                    Set up {{ entry.skillName || entry.skillId || 'skill' }}
                  </template>
                  <template v-else-if="entry.kind === 'channel_subscribe_request'">
                    Subscribe request: {{ entry.channelType || 'channel' }}
                  </template>
                  <template v-else>{{ entry.conversationTitle }}</template>
                </span>
                <span class="time">{{ formatTime(entry.createdAt) }}</span>
              </div>
              <div
                v-if="entry.kind === 'channel_otp'"
                class="preview otp-text"
              >
                <Icon icon="mdi:lock-outline" class="kind-icon" />
                <span class="otp-code">{{ entry.otp || entry.messagePreview }}</span>
                <span v-if="entry.senderName || entry.senderId" class="otp-sender">
                  · for {{ entry.senderName || entry.senderId }}
                </span>
              </div>
              <div
                v-else-if="entry.kind === 'skill_register_required'"
                class="preview skill-register-text"
              >
                <Icon icon="mdi:tools" class="kind-icon" />
                <span>{{ entry.messagePreview || 'Click to register the background process.' }}</span>
              </div>
              <div
                v-else
                class="preview" :class="{ 'error-text': entry.kind === 'error' }"
              >
                <Icon
                  v-if="entry.kind === 'error'"
                  icon="mdi:alert-circle-outline"
                  class="kind-icon"
                />
                <Icon
                  v-else
                  icon="mdi:check-circle-outline"
                  class="kind-icon"
                />
                <span>{{ entry.messagePreview || (entry.kind === 'error' ? 'Stream error' : 'Response ready') }}</span>
              </div>
            </div>
          </div>
        </TransitionGroup>
      </template>

      <template v-if="normalItems.length > 0">
        <div class="section-label">Other</div>
        <TransitionGroup name="notif-item" tag="div" class="section-group">
          <div
            v-for="entry in normalItems"
            :key="entry.id"
            class="notification-item"
            :class="{
              unseen: !entry.seen,
              error: entry.kind === 'error',
              otp: entry.kind === 'channel_otp',
              'skill-register': entry.kind === 'skill_register_required',
            }"
            @click="handleEntryClick(entry)"
          >
            <div class="notification-dot" :class="{ unseen: !entry.seen }" />
            <div class="notification-body">
              <div class="notification-row">
                <span class="conv-title">
                  <template v-if="entry.kind === 'channel_otp'">
                    OTP for {{ entry.channelType || 'channel' }}
                  </template>
                  <template v-else-if="entry.kind === 'skill_register_required'">
                    Set up {{ entry.skillName || entry.skillId || 'skill' }}
                  </template>
                  <template v-else-if="entry.kind === 'channel_subscribe_request'">
                    Subscribe request: {{ entry.channelType || 'channel' }}
                  </template>
                  <template v-else>{{ entry.conversationTitle }}</template>
                </span>
                <span class="time">{{ formatTime(entry.createdAt) }}</span>
              </div>
              <div
                v-if="entry.kind === 'channel_otp'"
                class="preview otp-text"
              >
                <Icon icon="mdi:lock-outline" class="kind-icon" />
                <span class="otp-code">{{ entry.otp || entry.messagePreview }}</span>
                <span v-if="entry.senderName || entry.senderId" class="otp-sender">
                  · for {{ entry.senderName || entry.senderId }}
                </span>
              </div>
              <div
                v-else-if="entry.kind === 'skill_register_required'"
                class="preview skill-register-text"
              >
                <Icon icon="mdi:tools" class="kind-icon" />
                <span>{{ entry.messagePreview || 'Click to register the background process.' }}</span>
              </div>
              <div
                v-else
                class="preview" :class="{ 'error-text': entry.kind === 'error' }"
              >
                <Icon
                  v-if="entry.kind === 'error'"
                  icon="mdi:alert-circle-outline"
                  class="kind-icon"
                />
                <Icon
                  v-else
                  icon="mdi:check-circle-outline"
                  class="kind-icon"
                />
                <span>{{ entry.messagePreview || (entry.kind === 'error' ? 'Stream error' : 'Response ready') }}</span>
              </div>
            </div>
          </div>
        </TransitionGroup>
      </template>
    </div>
  </div>
</template>

<style scoped>
.notification-list {
  display: flex;
  flex-direction: column;
  max-height: 420px;
  min-width: 280px;
}

.notification-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 4px 8px;
  border-bottom: 1px solid var(--border-color);
}

.notification-title {
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--text-primary);
}

.notification-actions {
  display: flex;
  gap: 2px;
}

.header-btn {
  background: transparent;
  border: none;
  color: var(--text-tertiary);
  cursor: pointer;
  width: 22px;
  height: 22px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  font-size: 14px;
  padding: 0;
}

.header-btn:hover {
  background: var(--hover-bg);
  color: var(--primary-color);
}

.notification-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 24px 12px;
  color: var(--text-tertiary);
  font-size: 0.85rem;
  gap: 8px;
}

.empty-icon {
  font-size: 28px;
  opacity: 0.6;
}

.notification-items {
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}

.notification-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 6px;
  cursor: pointer;
  border-radius: 6px;
  transition: background 0.15s ease;
  border-bottom: 1px solid var(--border-color);
}

.notification-item:last-child {
  border-bottom: none;
}

.notification-item:hover {
  background: var(--hover-bg);
}

.notification-item.unseen {
  background: rgba(37, 99, 235, 0.06);
}

.notification-item.unseen.error {
  background: rgba(239, 68, 68, 0.06);
}

.notification-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-top: 6px;
  background: transparent;
  flex-shrink: 0;
}

.notification-dot.unseen {
  background: var(--primary-color);
}

.notification-item.error .notification-dot.unseen {
  background: #ef4444;
}

.notification-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.notification-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.conv-title {
  font-size: 0.85rem;
  font-weight: 500;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.time {
  font-size: 0.7rem;
  color: var(--text-tertiary);
  flex-shrink: 0;
}

.preview {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 0.75rem;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.preview.error-text {
  color: #ef4444;
}

.preview.otp-text { color: var(--text-primary); align-items: baseline; }
.notification-item.otp.unseen { background: rgba(245, 158, 11, 0.08); }
.notification-item.otp .notification-dot.unseen { background: #f59e0b; }

.notification-item.skill-register.unseen { background: rgba(16, 185, 129, 0.08); }
.notification-item.skill-register .notification-dot.unseen { background: #10b981; }
.preview.skill-register-text { color: var(--text-primary); }
.otp-code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 1rem; font-weight: 600; letter-spacing: 0.08em;
}
.otp-sender { color: var(--text-secondary); font-size: 0.75rem; }

.kind-icon {
  font-size: 12px;
  flex-shrink: 0;
}

.preview span {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.section-label {
  font-size: 0.7rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-tertiary);
  padding: 8px 4px 4px;
  display: flex;
  align-items: center;
  gap: 4px;
}

.section-label-high {
  color: #ef4444;
}

.section-label-icon {
  font-size: 12px;
}

.section-group {
  display: flex;
  flex-direction: column;
}

.notif-item-high {
  border-left: 3px solid #ef4444;
  padding-left: 6px;
}

.notif-item-high.otp {
  border-left-color: #f59e0b;
}

.notif-item-high.skill-register {
  border-left-color: #10b981;
}

/* TransitionGroup arrival/leave animations */
.notif-item-enter-active {
  animation: notif-slide-in 0.35s ease;
}
.notif-item-leave-active {
  animation: notif-fade-out 0.2s ease forwards;
  position: absolute;
  width: 100%;
}
.notif-item-high.notif-item-enter-active {
  animation: notif-flash-in 0.5s ease;
}

@keyframes notif-slide-in {
  from { opacity: 0; transform: translateX(-12px); }
  to { opacity: 1; transform: translateX(0); }
}

@keyframes notif-flash-in {
  0% { opacity: 0; transform: translateX(-12px); background: rgba(239, 68, 68, 0.25); }
  60% { opacity: 1; transform: translateX(0); background: rgba(239, 68, 68, 0.18); }
  100% { background: transparent; }
}

@keyframes notif-fade-out {
  to { opacity: 0; transform: translateX(8px); }
}
</style>
