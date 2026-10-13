# Scores de v3 sobre las líneas de la flota

Corrida de `tools/score_fleet_lines.py` en la laptop de Facundo, el 13 de octubre de 2026, adentro del
contenedor del detector. Respalda la resolución del ticket 1 de `tasks.md`.

Modelo `modernbert-ft-v3` revisión `local`, umbral 0.5

| escenario | se espera | score | marcada | ok | línea |
|---|---|---|---|---|---|
| panic | pass | 0.0053 | no | ok | `Oct 13 10:14:05 db1 kernel: Kernel panic - not syncing: Fatal exception in interrupt` |
| panic | pass | 0.0021 | no | ok | `Oct 13 10:14:05 db1 kernel: Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(0,0)` |
| panic | pass | 0.0014 | no | ok | `Oct 13 10:14:05 db1 kernel: BUG: unable to handle page fault for address: ffffffffc0a1b2c3` |
| panic | pass | 0.0036 | no | ok | `Oct 13 10:14:05 db1 kernel: Oops: 0000 [#1] PREEMPT SMP PTI` |
| panic | pass | 0.0068 | no | ok | `Oct 13 10:14:05 db1 kernel: RIP: 0010:ext4_es_lookup_extent+0x6d/0x230` |
| panic | pass | 0.0143 | no | ok | `Oct 13 10:14:05 db1 kernel: ---[ end Kernel panic - not syncing: Fatal exception in interrupt ]---` |
| operational | pass | 0.0017 | no | ok | `Oct 13 10:14:05 db1 kernel: Out of memory: Killed process 4211 (postgres) total-vm:8123456kB, anon-rss:6012344kB` |
| operational | pass | 0.0013 | no | ok | `Oct 13 10:14:05 db1 kernel: EXT4-fs error (device sda1): ext4_find_entry:1455: inode #2: comm postgres: reading directory lblock 0` |
| operational | pass | 0.0963 | no | ok | `Oct 13 10:14:05 db1 kernel: blk_update_request: I/O error, dev sda, sector 123456 op 0x1:(WRITE) flags 0x800 phys_seg 1 prio class 0` |
| operational | pass | 0.0002 | no | ok | `Oct 13 10:14:05 db1 postgres[901]: PANIC:  could not write to file "pg_wal/xlogtemp.901": No space left on device` |
| operational | pass | 0.0019 | no | ok | `Oct 13 10:14:05 db1 systemd[1]: postgresql.service: Main process exited, code=killed, status=9/KILL` |
| operational | pass | 0.0239 | no | ok | `Oct 13 10:14:05 db1 systemd[1]: postgresql.service: Failed with result 'oom-kill'.` |
| operational | pass | 0.0000 | no | ok | `Oct 13 10:14:05 mail1 postfix/smtpd[2210]: warning: not enough free space in mail queue: 10485760 bytes < 1.5*message size limit` |
| background | flag | 1.0000 | sí | ok | `Oct 13 10:14:05 db1 sshd[3411]: Failed password for root from 203.0.113.41 port 52144 ssh2` |
| background | flag | 1.0000 | sí | ok | `Oct 13 10:14:05 db1 sshd[3412]: Invalid user oracle from 198.51.100.23 port 40122` |
| background | flag | 0.0004 | no | FALLA | `Oct 13 10:14:05 db1 sshd[3413]: Connection closed by authenticating user root 203.0.113.41 port 52150 [preauth]` |
| background | flag | 0.9944 | sí | ok | `Oct 13 10:14:05 db1 postgres[1201]: FATAL:  password authentication failed for user "postgres"` |
| background | flag | 0.0001 | no | FALLA | `Oct 13 10:14:05 mail1 postfix/smtpd[2211]: warning: unknown[203.0.113.77]: SASL LOGIN authentication failed: UGFzc3dvcmQ6` |
| background | flag | 0.0000 | no | FALLA | `Oct 13 10:14:05 mail1 postfix/smtpd[2212]: NOQUEUE: reject: RCPT from unknown[198.51.100.9]: 554 5.7.1 <spam@example.net>: Relay access denied; from=<a@b.c> to=<spam@example.net> proto=ESMTP helo=<x>` |
| background | flag | 1.0000 | sí | ok | `Oct 13 10:14:05 mail1 sshd[3420]: Failed password for invalid user test from 192.0.2.66 port 33012 ssh2` |
| background | flag | 1.0000 | sí | ok | `198.51.100.14 - - [13/Oct/2026:10:14:05 +0000] "GET /wp-login.php HTTP/1.1" 404 162 "-" "Mozilla/5.0"` |
| background | flag | 0.0010 | no | FALLA | `198.51.100.14 - - [13/Oct/2026:10:14:06 +0000] "GET /.git/config HTTP/1.1" 404 162 "-" "Mozilla/5.0"` |
| background | flag | 0.9931 | sí | ok | `198.51.100.14 - - [13/Oct/2026:10:14:07 +0000] "GET /phpmyadmin/ HTTP/1.1" 404 162 "-" "Mozilla/5.0"` |
| background | flag | 0.7449 | sí | ok | `198.51.100.14 - - [13/Oct/2026:10:14:08 +0000] "POST /xmlrpc.php HTTP/1.1" 403 162 "-" "Mozilla/5.0"` |
| normal | pass | 0.0034 | no | ok | `Oct 13 10:14:05 db1 postgres[901]: LOG:  checkpoint starting: time` |
| normal | pass | 0.0038 | no | ok | `Oct 13 10:14:05 db1 postgres[901]: LOG:  checkpoint complete: wrote 412 buffers (2.5%); 0 WAL file(s) added, 0 removed, 1 recycled` |
| normal | pass | 0.0042 | no | ok | `Oct 13 10:14:05 db1 postgres[1300]: LOG:  connection authorized: user=app database=shop application_name=api` |
| normal | pass | 0.0056 | no | ok | `Oct 13 10:14:05 db1 sshd[3500]: Accepted publickey for deploy from 10.0.0.9 port 51234 ssh2: ED25519 SHA256:abc` |
| normal | pass | 0.0000 | no | ok | `Oct 13 10:14:05 mail1 postfix/smtp[2300]: 4F2A11C0D3: to=<ventas@cliente.com.ar>, relay=mx.cliente.com.ar[10.0.0.30]:25, delay=0.4, status=sent (250 2.0.0 Ok: queued)` |
| normal | pass | 0.0000 | no | ok | `Oct 13 10:14:05 mail1 postfix/qmgr[2301]: 4F2A11C0D3: removed` |
| normal | pass | 0.0002 | no | ok | `Oct 13 10:14:05 mail1 systemd[1]: Started Daily apt download activities.` |
| normal | pass | 0.0002 | no | ok | `Oct 13 10:14:05 mail1 CRON[3120]: pam_unix(cron:session): session opened for user root(uid=0) by (uid=0)` |

panic: 6/6 como se esperaba
operational: 7/7 como se esperaba
background: 7/11 como se esperaba
normal: 8/8 como se esperaba
