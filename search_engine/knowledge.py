"""领域知识库 — 为 LLM 查询生成提供术语、同义词、核心物质等上下文。

后续会扩展为完整的 Obsidian 笔记 → 知识库检索，目前是硬编码模板。
"""

# ── 光固化材料 ────────────────────────────────────────

PHOTOCURING_CONTEXT = """
## Photocuring Materials Domain Knowledge

### Synonyms & Variants
- photocuring = photopolymerization = light-curing = UV-curing = light-induced polymerization = actinic light curing = photo-curing = photo curing = visible light curing = photo-initiated polymerization = light-activated polymerization
- photocurable = light-curable = UV-curable = photosensitive = light-curable resin = photocurable resin
- photopolymerizable = photo-polymerizable = light-polymerizable

### Shrinkage-Related Terms (important — many variants)
- polymerization shrinkage = volumetric shrinkage = polymerization contraction = volume shrinkage = shrinkage behavior = curing shrinkage = shrinkage strain
- shrinkage stress = contraction stress = polymerization stress = curing stress = residual stress
- degree of conversion = double bond conversion = conversion rate = monomer conversion = vinyl conversion = C=C conversion

### Core Substances (Monomers, Photoinitiators, Fillers)
- Monomers: Bis-GMA, TEGDMA, UDMA, Bis-EMA, PMMA, Tetric, methacrylate, acrylate
- Photoinitiators: camphorquinone (CQ), Lucirin TPO, Irgacure 819, DMPA, benzophenone, phenylpropanedione (PPD), ethyl 4-dimethylaminobenzoate (EDMAB)
- Fillers: silica, zirconia, barium glass, hydroxyapatite, graphene oxide, MXene, CNT, nanoclay, alumina
- Co-initiators: DMAEMA, DMAB, EDMAB

### Core Metrics & Properties
- degree of conversion (DC) — use PRE/2 or PRE/3 for multi-word: degree PRE/2 conversion
- polymerization shrinkage — use: polymerization PRE/3 shrinkage
- shrinkage stress, contraction stress, curing stress
- depth of cure, curing depth
- mechanical properties: flexural strength, flexural modulus, compressive strength, microhardness, Vickers hardness
- water sorption, solubility, hydrolytic degradation
- biocompatibility, cytotoxicity
- oxygen inhibition layer, oxygen-inhibited layer
- double bond conversion
- gel point, vitrification

### Key Techniques
- FTIR (Fourier transform infrared spectroscopy) — for degree of conversion
- DSC (differential scanning calorimetry) — photocalorimetry, photo-DSC
- DMA (dynamic mechanical analysis)
- SEM, TEM — morphology
- micro-CT — 3D structure

### Application Areas
- Dental composites, dental adhesives, dental restorative materials
- 3D printing, DLP (digital light processing), SLA (stereolithography), vat photopolymerization
- Bone cement, orthopedic cement, bioactive composites
- Coatings, protective coatings, anti-corrosion coatings
- Hydrogels, tissue engineering scaffolds, bioprinting
- Microfluidics, electronics encapsulation

### Common Research Routes
1. Formulation optimization: varying monomer ratios, photoinitiator concentrations, filler loading
2. Surface modification: silanization of fillers, functionalization
3. New photoinitiator systems: multi-component initiators, visible light initiators
4. Nanocomposites: nanoparticle fillers, hybrid materials
5. Process optimization: light intensity, exposure time, curing protocol

### Opposing Views & Limitations
- High filler loading improves mechanical properties but reduces depth of cure
- Fast curing → high shrinkage stress → interfacial gaps
- camphorquinone causes yellowing (esthetic concerns for dental)
- Oxygen inhibition reduces surface conversion
- Bis-GMA has high viscosity → needs diluent (TEGDMA) but TEGDMA increases shrinkage
- Bulk-fill composites: depth of cure claims vs actual clinical performance
- Alternative: silorane-based low-shrinkage composites
- Alternative: self-adhesive composites vs traditional etch-and-rinse
"""

# ── 机器学习（占位） ──────────────────────────────────

ML_CONTEXT = """
## Machine Learning Domain Knowledge

### Synonyms
- machine learning = ML = statistical learning = pattern recognition
- deep learning = neural networks = DNN
- large language model = LLM = foundation model = transformer model

### Core Concepts
- supervised learning, unsupervised learning, reinforcement learning
- classification, regression, clustering, dimensionality reduction
- overfitting, regularization, cross-validation, hyperparameter tuning
- bias-variance tradeoff, generalization error

### Common Models
- Random Forest, XGBoost, SVM, Logistic Regression
- CNN, RNN, LSTM, Transformer, GNN
- Autoencoder, GAN, Diffusion Model

### Metrics
- accuracy, precision, recall, F1-score, AUC-ROC
- MSE, MAE, RMSE, R-squared
- perplexity, BLEU, ROUGE
- inference time, FLOPs, parameter count

### Key Venues
- NeurIPS, ICML, ICLR, CVPR, ACL, AAAI, JMLR, TPAMI
"""

# ── 电机/机器人（占位） ───────────────────────────────

MOTOR_CONTEXT = """
## Motor & Robotics Domain Knowledge

### Motor Types
- PMSM (permanent magnet synchronous motor), BLDC (brushless DC)
- induction motor, stepper motor, servo motor
- switched reluctance motor, axial flux motor

### Control Methods
- FOC (field-oriented control), DTC (direct torque control)
- MPC (model predictive control), sliding mode control
- PID control, adaptive control, robust control
- sensorless control, encoderless control

### Robotics
- forward/inverse kinematics, dynamics modeling
- trajectory planning, motion control
- SLAM, path planning, obstacle avoidance
- force control, impedance control, compliance control

### Key Metrics
- torque ripple, efficiency, power density
- settling time, overshoot, steady-state error
- positioning accuracy, repeatability
- THD (total harmonic distortion), power factor
"""


# ── 领域上下文获取 ─────────────────────────────────────

DOMAINS = {
    "photocuring": PHOTOCURING_CONTEXT,
    "ml": ML_CONTEXT,
    "motor": MOTOR_CONTEXT,
}


def get_domain_context(domain: str) -> str:
    """获取指定领域的知识库上下文。"""
    return DOMAINS.get(domain, PHOTOCURING_CONTEXT)
