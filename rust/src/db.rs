//! Postgres connection pool (`deadpool_postgres`) & shared `LISTEN/NOTIFY` hub (`db.rs`).

use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod, Runtime};
use futures_util::stream;
use futures_util::StreamExt;
use std::str::FromStr;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Notify;
use tokio_postgres::{AsyncMessage, NoTls};

pub fn build_pool(dsn: &str, max_size: usize) -> Result<Pool, String> {
    let pg_cfg = tokio_postgres::Config::from_str(dsn)
        .map_err(|e| format!("invalid MEMNOS_DSN {:?}: {}", dsn, e))?;
    let mgr_cfg = ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    };
    let mgr = Manager::from_config(pg_cfg, NoTls, mgr_cfg);
    Pool::builder(mgr)
        .max_size(max_size.max(4))
        .wait_timeout(Some(Duration::from_secs(10)))
        .runtime(Runtime::Tokio1)
        .build()
        .map_err(|e| format!("failed to build Postgres pool: {}", e))
}

/// Spawn ONE background `LISTEN memnos_channel` connection that wakes all active `/channel/wait`
/// tasks via `Arc<Notify>` in `<2ms` (including cross-namespace `global_topic` channels).
pub fn spawn_channel_listener(dsn: String, notify: Arc<Notify>) {
    tokio::spawn(async move {
        loop {
            match tokio_postgres::connect(&dsn, NoTls).await {
                Ok((client, mut conn)) => {
                    let notify_inner = notify.clone();
                    let stream_task = tokio::spawn(async move {
                        let mut messages =
                            stream::poll_fn(move |cx| conn.poll_message(cx));
                        while let Some(Ok(msg)) = messages.next().await {
                            if let AsyncMessage::Notification(_) = msg {
                                notify_inner.notify_waiters();
                            }
                        }
                    });
                    if client.batch_execute("LISTEN memnos_channel").await.is_ok() {
                        let _ = stream_task.await;
                    }
                }
                Err(_) => {}
            }
            tokio::time::sleep(Duration::from_secs(2)).await;
        }
    });
}
