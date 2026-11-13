# Le Fil, version GitHub

Agrégateur d'actualité par flux RSS, sans algorithme : 20 sources tech et bidouille,
5 sources généralistes, liste chronologique. Tout tourne gratuitement chez GitHub :

- toutes les 10 minutes environ, GitHub Actions exécute `releve.py`, qui relève les flux ;
- le résultat est publié sur GitHub Pages avec la page ;
- la page relit les données chaque minute et fait glisser les nouveaux articles en haut,
  surlignés 15 secondes.

Aucune machine à laisser allumée chez toi.

## Mise en ligne (une seule fois, environ 10 minutes)

1. **Créer le dépôt.** Sur https://github.com/new : nom `le-fil`, visibilité **Public**
   (indispensable pour que ce soit gratuit), sans README.

2. **Envoyer les fichiers** depuis le dossier décompressé :

       cd le-fil
       git init -b main
       git add .
       git commit -m "Le Fil"
       git remote add origin https://github.com/TON-COMPTE/le-fil.git
       git push -u origin main

   Si Git demande un mot de passe, utilise `gh auth login` (paquet `gh`) ou un jeton personnel
   (Settings > Developer settings > Personal access tokens). Le relevé lancé par cet envoi
   échouera à l'étape de publication : c'est normal, Pages n'est pas encore activé.

3. **Activer Pages.** Dans le dépôt : Settings > Pages > Build and deployment > Source :
   **GitHub Actions**.

4. **Lancer le premier relevé.** Onglet Actions > « Relevé des flux » > Run workflow.
   Une minute plus tard, le site est en ligne à l'adresse
   `https://TON-COMPTE.github.io/le-fil/`.

5. **Sur le téléphone**, ouvre cette adresse puis :
   - iPhone (Safari) : bouton Partager > Sur l'écran d'accueil ;
   - Android (Chrome) : menu ⋮ > Ajouter à l'écran d'accueil (ou Installer l'application).

   Le Fil s'ouvre alors en plein écran, comme une appli.

## Au quotidien

- **Le point en haut de la page** : vert avec l'heure du dernier relevé, orange si le relevé
  a plus de 45 minutes de retard. Un clic dessus affiche l'état de chaque source
  (nombre d'articles, erreur éventuelle).
- **Modifier les sources** : édite `sources.toml` directement sur GitHub (icône crayon), puis
  Commit. Un relevé se lance aussitôt avec la nouvelle liste. `max_par_heure` sur les sites
  généralistes règle le ratio tech / généraliste.
- **Le détail de chaque relevé** : onglet Actions > un passage > résumé en bas de page,
  avec un tableau source par source.

## Comment c'est construit

- `releve.py` relève tous les flux en parallèle avec des requêtes conditionnelles : quand
  rien n'a changé, le site répond sans renvoyer le flux. Si l'adresse d'un flux ne répond
  plus, il cherche celle annoncée sur la page d'accueil du site. Les articles sans image
  prennent l'image de partage de leur page.
- La mémoire du relevé (articles, doublons, caches) est gardée dans la branche `etat`,
  réécrite à chaque passage en un seul commit : le dépôt ne grossit pas.
- La première fois qu'une source est relevée, ses articles s'ajoutent sans animation,
  pour ne pas noyer la page.
- Le workflow appelle à chaque passage planifié l'API qui remet à zéro le compteur
  d'inactivité de GitHub (les tâches planifiées d'un dépôt public sont sinon coupées
  au bout de 60 jours sans activité).

## Tester sur ton PC

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python releve.py
    python3 -m http.server -d site 8000

Puis http://localhost:8000. L'adresse `…/?demo` affiche des articles fictifs, pratique pour
retoucher le design.

## Limites à connaître

- **Pas du temps réel.** GitHub peut retarder les tâches planifiées, surtout en début
  d'heure : compter 10 à 30 minutes entre la publication d'un article et son arrivée.
  Tu peux passer le cron à `*/5` dans `.github/workflows/releve.yml`, c'est le minimum.
- **Conditions d'utilisation.** GitHub demande que les Actions servent à construire,
  tester ou publier le projet du dépôt. Ici, le workflow produit et publie le contenu du
  site, ce qui reste défendable, mais c'est une zone grise.
- **Tout est public** : le dépôt, la liste des sources et le site (une simple liste de liens).

## En cas de souci

- **Point orange qui dure** : onglet Actions. Si le workflow affiche « disabled »,
  clique sur Enable workflow. Sinon, ouvre le dernier passage pour lire l'erreur.
- **Erreur 403 à l'étape « Sauvegarder la mémoire »** : Settings > Actions > General >
  Workflow permissions > Read and write permissions.
- **Une source reste en erreur** : le site bloque peut-être les robots, ou son flux a
  changé d'adresse sans l'annoncer. Corrige ou retire-la dans `sources.toml`.
