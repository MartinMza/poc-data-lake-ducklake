"""Adquisición automática del backup de SQL Server desde el correo.

Paquete del componente de orquestación: no importa Prefect ni el motor. El
entrypoint lo compone `orchestration/flow.py`.

Módulos:
    identity   identidad y orden del backup (fecha codificada en el nombre)
    state      estado persistido entre corridas (Message-IDs, último aplicado)
    download   descarga reanudable + integridad + commit atómico
    mailbox    poller IMAP de Gmail y extracción de links
    resolvers  resolución del link por host, con allowlist
"""
