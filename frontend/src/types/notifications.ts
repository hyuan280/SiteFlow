export type NotificationSource = "env" | "db" | "unset";

export interface NotificationFieldView {
  value: unknown;
  source: NotificationSource;
  editable: boolean;
  error: string | null;
}

export interface NotificationSecretView {
  configured: boolean;
  source: NotificationSource;
  editable: boolean;
  masked: string | null;
}

export interface NotificationLayout {
  title_template: string;
  body_template: string;
  format: "text" | "html";
  editable: boolean;
}

export interface EmailNotificationView {
  enabled: boolean;
  notify_success: boolean;
  notify_manual: boolean;
  available: boolean;
  smtp_host: NotificationFieldView;
  smtp_port: NotificationFieldView;
  security: NotificationFieldView;
  username: NotificationFieldView;
  password: NotificationSecretView;
  from_address: NotificationFieldView;
  to_addresses: NotificationFieldView;
  error: string | null;
}

export interface HTTPNotificationView {
  enabled: boolean;
  notify_success: boolean;
  notify_manual: boolean;
  available: boolean;
  url: NotificationFieldView;
  method: NotificationFieldView;
  headers: NotificationFieldView;
  header_names: string[];
  timeout_seconds: NotificationFieldView;
  error: string | null;
}

export interface NotificationSettings {
  layout: NotificationLayout;
  email: EmailNotificationView;
  http: HTTPNotificationView;
}

export interface DeliveryResult {
  channel: string;
  ok: boolean;
  message: string;
}

export interface NotificationTestResult {
  ok: boolean;
  message: string;
  results: DeliveryResult[];
}
