export interface MatrixCellView {
  status: string;
  attempts: number;
  counts: Record<string, number>;
  last_run_id: number | null;
  last_job_id: number | null;
  last_message: string;
}

export interface MatrixSiteView {
  site_id: number;
  name: string;
  enabled: boolean;
  cells: Record<string, MatrixCellView>;
}

export interface MatrixView {
  start_date: string;
  end_date: string;
  dates: string[];
  sites: MatrixSiteView[];
}

export interface FailureStepView {
  step_key: string;
  label: string;
  feature_type: string;
  status: string;
  message: string;
}

export interface FailureItemView {
  job_id: number;
  run_id: number;
  site_id: number | null;
  site_name: string;
  lane: string;
  attempt: number;
  status: string;
  message: string;
  started_at: string | null;
  finished_at: string | null;
  failed_steps: FailureStepView[];
  screenshot_id: number | null;
}

export interface FailuresView {
  total: number;
  page: number;
  page_size: number;
  items: FailureItemView[];
}

export interface TrendPointView {
  date: string;
  total: number;
  success: number;
  fail: number;
  error: number;
  waiting_human: number;
  unknown: number;
  deferred: number;
  canceled: number;
  interrupted: number;
  success_rate: number;
}

export interface TrendsView {
  start_date: string;
  end_date: string;
  points: TrendPointView[];
  total: number;
  success: number;
  fail: number;
  error: number;
  waiting_human: number;
  unknown: number;
  deferred: number;
  canceled: number;
  interrupted: number;
  success_rate: number;
}
