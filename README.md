# Syslog pour Home Assistant OS (fork HL42)

![Supports aarch64 Architecture][aarch64-shield] ![Supports amd64 Architecture][amd64-shield]

Application (module) Home Assistant OS qui envoie **tout le journal de HAOS**
à un serveur syslog distant, et qui **reprend sans perte** après une coupure du
serveur ou un redémarrage.

## Origine

Ce dépôt est un fork de
[mib1185/ha-addon-syslog](https://github.com/mib1185/ha-addon-syslog),
version 0.6.0, sous licence Apache 2.0 (fichier `LICENSE` conservé). Le script
d'envoi `syslog/journal2syslog.py` a été réécrit en gardant les mêmes formats
et options ; `config.yaml`, `run.sh`, `Dockerfile`, `DOCS.md` et les
traductions ont été modifiés. Le détail est dans `syslog/CHANGELOG.md`.

Corrections reprises d'autres contributeurs, avec leur crédit :

| Apport | Auteur | Source |
|---|---|---|
| Détection d'une connexion TCP morte (délai, keepalive, `TCP_USER_TIMEOUT`) | tb-dev-ops | [demande de fusion n° 52](https://github.com/mib1185/ha-addon-syslog/pull/52) |
| Option `redact_patterns` (masquage des secrets) | Milas22 | [ticket n° 45](https://github.com/mib1185/ha-addon-syslog/issues/45) |
| Nom du conteneur comme nom d'application | bombadi | [fork](https://github.com/bombadi/ha-addon-syslog) |
| Retrait des couleurs composées (`\x1b[1;31m`) | w531t4 | [fork](https://github.com/w531t4/ha-addon-syslog) |

## Finalité

HAOS ne sait pas envoyer ses journaux à un serveur syslog. Le module d'origine
le fait, mais perd des lignes sans le signaler :

- tout ce qui s'écrit pendant que le module est arrêté, dont le démarrage de
  HAOS, puisqu'il repart de la fin du journal ;
- la ligne en cours à chaque erreur d'envoi, sans nouvel essai ;
- jusqu'à environ 15 minutes de journaux quand le serveur disparaît sans
  fermer la connexion TCP.

Ce fork se sert du journal de HAOS comme tampon : une entrée n'est
considérée comme envoyée qu'une fois remise au serveur, et la position dans le
journal est enregistrée dans le stockage du module (`/data`). Il apporte en
outre :

- l'heure de l'événement, et non celle de l'envoi ;
- la gravité lue dans les lignes des modules (bashio, s6-overlay, Python,
  logfmt, Zigbee2MQTT, Mosquitto, OpenThread) et le niveau `notice` conservé ;
- le masquage des secrets avant qu'ils quittent l'hôte ;
- une ligne dans le journal du module quand le serveur devient injoignable,
  une quand il revient ;
- un banc d'essai hors ligne (`tests/`).

Limites connues : après un arrêt brutal, les entrées des 5 dernières secondes
peuvent être envoyées deux fois ; une connexion TCP morte est détectée en
environ 30 secondes, et ce qui lui a été confié pendant ce temps peut être
perdu ; en UDP, une perte n'est jamais vue. Le détail des options et du
comportement est dans `syslog/DOCS.md`, affiché aussi dans l'onglet
*Documentation* du module.

## Installation

Aucune image n'est publiée : le Supervisor construit l'image Docker sur
l'hôte Home Assistant à partir de `syslog/Dockerfile` (Debian 13, image de
base fixée), à l'installation et à chaque reconstruction.

### Module local (recommandé tant que le dépôt est privé)

1. Copier le dossier `syslog` dans le partage `addons` de Home Assistant
   (module Samba ou SSH), de sorte que `addons/syslog/config.yaml` existe.
   La construction rétablit d'elle-même le bit d'exécution et les fins de
   ligne des scripts, qu'une copie depuis Windows peut altérer.
2. *Paramètres → Applications → Installer une application → ⋮ → Rechercher
   des mises à jour* : le module apparaît sous **Applications locales**, sous
   le nom *Syslog (HL42)*.
3. L'installer, puis régler les options, par exemple :

   ```yaml
   syslog_host: 192.0.2.10   # adresse IP du serveur : pas de dépendance au DNS
   syslog_port: 1514
   syslog_protocol: tcp
   syslog_ssl: false
   syslog_ssl_verify: true
   syslog_format: rfc5424
   redact_patterns: []
   backlog_max_hours: 6
   ```

4. Activer le *watchdog*, désactiver la mise à jour automatique, démarrer.
5. Vérifier dans l'onglet *Journal* : `forwarding to …` puis aucune ligne
   `cannot send`.

Mise à jour : recopier le dossier en augmentant `version` dans
`config.yaml`, puis *Mettre à jour* (ou *Reconstruire*). La position
enregistrée survit aux mises à jour ; elle est effacée à la désinstallation,
et le module repart alors de la fin du journal.

### Depuis la boutique d'applications

*Paramètres → Applications → Installer une application → ⋮ → Dépôts*, puis
ajouter l'URL de ce dépôt. Cela fonctionne simplement si le dépôt est
**public**. Un dépôt privé exige un jeton GitHub dans l'URL
(`https://<jeton>@github.com/…`), que le Supervisor conserve en clair ; cette
méthode échoue en outre sur le Supervisor 2026.10
([supervisor#7453](https://github.com/home-assistant/supervisor/issues/7453)).

## Tests

Le banc lance le vrai script contre un faux journal et un récepteur syslog
local ; il ne demande que Python 3 :

```sh
python -m unittest discover -s tests -v
```

`J2S_SCRIPT=/chemin/vers/journal2syslog.py` lance le même banc contre une
autre version du script, par exemple celle d'origine.

## Contenu du dépôt

| Chemin | Rôle |
|---|---|
| `repository.yaml` | description du dépôt pour la boutique d'applications |
| `syslog/` | le module : `config.yaml`, `Dockerfile`, `run.sh`, `journal2syslog.py`, documentation et traductions |
| `tests/` | banc d'essai hors ligne et faux `systemd.journal` |
| `.gitattributes` | fins de ligne LF imposées, le dossier `syslog` étant copié tel quel vers HAOS |

## Licence

Apache License 2.0, comme le projet d'origine. Voir `LICENSE`.

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
