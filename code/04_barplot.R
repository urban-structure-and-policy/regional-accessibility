library(dplyr)
library(terra)
library(ggplot2)
library(readxl)
library(openxlsx)
library(tidyverse)
library(scales)

options(scipen=999)

english_format <- label_number(big.mark = ",", decimal.mark = ".")

vect_path <- file.path(my_wd, 'output', 'vacant_lots', 'vacant_lots_accessibility.gpkg')
shape <- vect(vect_path, layer = "vacant_lots_accessibility")

df0<-as.data.frame(shape)
df<-df0
colnames(df)[2]<-"area_ha"
df$area_ha <-df$area_ha/10000

### aggregation ###

df$CC_mean<-round(df$CC_mean,3)

#Setzt die Anzahl der Kategorien
inter= 20

df_f<-df
df_f$CC_mean_int<- cut_interval(df_f[,"CC_mean"],n=inter,boundary = 0)
df_f<-na.omit(df_f)

head(df_f)

### plot ###

l<- c("lowest","","medium","","highest")
l<- c("\n   lowest",rep("",round(inter/2,0)-2),"",rep("",round(inter/2,0)-1),"\nhighest    ")
length(l)


# facet labels
pretty_labels <- c(
  "aachen" = "Aachen",
  "muenchen" = "München",
  "nordhessen" = "Nordhessen (Kassel)",
  "oberlausitz_niederschlesien" = "Oberlausitz-Niederschlesien"
)


p <- ggplot(df_f, aes(CC_mean_int, area_ha)) +
  theme_bw() +
  # Set white background
  theme(
    panel.background = element_rect(fill = "white", colour = NA),
    plot.background = element_rect(fill = "white", colour = NA),
    strip.background=element_rect(fill="white", colour = NA),
    # Font settings
    text = element_text(family = "Segoe UI", size = 8.5),
    axis.text = element_text(size = 7, family = "Segoe UI"),
    axis.text.x = element_text(face = "italic", angle = 0, hjust = 0.5),
    axis.title = element_text(size = 8.5, family = "Segoe UI"),
    # Horizontal grid lines only
    panel.grid.major.x = element_blank(),
    panel.grid.major.y = element_line(colour = "grey80", linewidth = 0.3),
    panel.grid.minor = element_blank(),
    # Facet labels with custom pretty names
    strip.text = element_text(size = 8.5, family = "Segoe UI", hjust = 0.5, face = "bold", margin = margin(t = 0.5, b = 0.8)),
    # Legend position
    legend.position = "bottom"
  ) +
  theme(
    # Increase space between axis and labels
    #axis.ticks.length.x = unit(0.5, "cm") # Extra space at bottom
  ) +
  # Use the custom labels for facets
  facet_wrap(~Gem_layer, ncol = 1, strip.position = "top", labeller = as_labeller(pretty_labels)) +
  # Geom and labels
  geom_col(fill = "#6ababb", alpha = 0.9) +
  scale_x_discrete(
    name = "Closeness Centrality [regional score]",
    labels = l  # Use your original `l` list for x-axis labels
  ) +
  labs(
    x = "Closeness Centrality [regional score]",
    y = "Area of vacant lots [ha]"
  )

dir.create(file.path(my_wd, "output", "barplot"), showWarnings = FALSE, recursive = TRUE)
out <- file.path(my_wd, "output", "barplot", paste0("barplot_", Sys.Date(), ".png"))
ggsave(plot=p,out, width = 8.3,height = 4,units = "cm",dpi = 300)

